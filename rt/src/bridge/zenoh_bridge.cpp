// SPDX-License-Identifier: GPL-3.0-or-later
#include "maxmount/bridge/zenoh_bridge.hpp"

#include <deque>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <utility>

#include "zenoh.hxx"

namespace maxmount::bridge {

namespace {

std::string json_string(const std::string& s) {
    std::string out = "\"";
    for (char c : s) {
        if (c == '"' || c == '\\') out += '\\';
        out += c;
    }
    return out + "\"";
}

std::string json_list(const std::vector<std::string>& v) {
    std::string out = "[";
    for (std::size_t i = 0; i < v.size(); ++i) out += (i ? "," : "") + json_string(v[i]);
    return out + "]";
}

zenoh::Config make_config(const config::ZenohConfig& zc) {
    if (!zc.config_file.empty()) return zenoh::Config::from_file(zc.config_file);
    zenoh::Config c = zenoh::Config::create_default();
    c.insert_json5("mode", json_string(zc.mode));
    if (!zc.connect.empty()) c.insert_json5("connect/endpoints", json_list(zc.connect));
    if (!zc.listen.empty()) c.insert_json5("listen/endpoints", json_list(zc.listen));
    c.insert_json5("scouting/multicast/enabled", zc.multicast_scouting ? "true" : "false");
    return c;
}

}  // namespace

struct ZenohBridge::Impl {
    BridgeOptions opt;
    std::string prefix;

    // Shared between Zenoh threads and the main thread.
    mutable std::mutex mu;
    std::deque<Command> pending;
    TelemetrySample last{};
    BridgeStats stats;

    // Main thread only.
    nanos tel_every_cycles = 20;
    std::uint64_t next_tel_cycle = 0;
    bool have_tel = false;
    std::unique_ptr<BatchEncoder> raw;

    std::optional<zenoh::Session> session;
    std::optional<zenoh::Publisher> pub_tel, pub_raw;
    std::optional<zenoh::Subscriber<void>> sub_traj;
    std::optional<zenoh::Queryable<void>> q_cmd;

    std::string key(const std::string& suffix) const { return prefix + suffix; }

    void reject_locked() { ++stats.rejected; }

    void on_trajectory(const zenoh::Sample& sample) {
        const std::vector<std::uint8_t> bytes = sample.get_payload().as_vector();
        Decoded d = decode_trajectory(
            std::string_view(reinterpret_cast<const char*>(bytes.data()), bytes.size()), opt.axis_names);
        std::lock_guard lock(mu);
        if (!opt.accept_commands || !d.ok() || pending.size() + d.commands.size() > opt.max_pending) {
            reject_locked();
            return;
        }
        pending.insert(pending.end(), d.commands.begin(), d.commands.end());
        ++stats.trajectories;
    }

    std::pair<bool, std::string> handle_command(const zenoh::Query& query) {
        const auto payload = query.get_payload();
        if (!payload) return {false, "MountCommand: empty query payload"};
        const std::vector<std::uint8_t> bytes = payload->get().as_vector();
        Decoded d = decode_command(std::string_view(reinterpret_cast<const char*>(bytes.data()), bytes.size()),
                                   opt.axis_names);
        if (!d.ok()) return {false, d.error};
        if (!opt.accept_commands) return {false, opt.reject_reason};

        std::lock_guard lock(mu);
        const Command& c = d.commands.front();
        if (opt.validate) {
            if (std::string why = opt.validate(c, last); !why.empty()) return {false, why};
        }
        // Stop commands jump the queue: buffered trajectory points must not delay them.
        if (c.type == CommandType::QuickStop || c.type == CommandType::Disable) {
            pending.push_front(c);
        } else if (pending.size() >= opt.max_pending) {
            return {false, "command queue full"};
        } else {
            pending.push_back(c);
        }
        return {true, "queued"};
    }

    void on_query(const zenoh::Query& query) {
        std::pair<bool, std::string> r{false, "internal error"};
        try {
            r = handle_command(query);
        } catch (const std::exception& e) {
            r = {false, std::string("internal error: ") + e.what()};
        }
        {
            std::lock_guard lock(mu);
            if (r.first) {
                ++stats.commands;
            } else {
                reject_locked();
            }
        }
        try {
            query.reply(zenoh::KeyExpr(key("mount/cmd")), zenoh::Bytes(encode_reply(r.first, r.second)));
        } catch (const std::exception&) {
            // The querier may be gone; nothing else to do.
        }
    }
};

ZenohBridge::ZenohBridge(BridgeOptions options) : impl_(std::make_unique<Impl>()) {
    impl_->opt = std::move(options);
    impl_->prefix = "maxmount/" + impl_->opt.zenoh.site + "/";
    const nanos cycle = impl_->opt.cycle > 0 ? impl_->opt.cycle : 1'000'000;
    impl_->tel_every_cycles = std::max<nanos>(1, impl_->opt.telemetry_period / cycle);
    impl_->raw = std::make_unique<BatchEncoder>(impl_->opt.axis_names);
}

ZenohBridge::~ZenohBridge() { stop(); }

void ZenohBridge::start() {
    Impl& m = *impl_;
    try {
        m.session.emplace(zenoh::Session::open(make_config(m.opt.zenoh)));
        m.pub_tel.emplace(m.session->declare_publisher(zenoh::KeyExpr(m.key("mount/telemetry"))));
        if (m.opt.zenoh.publish_raw) {
            m.pub_raw.emplace(m.session->declare_publisher(zenoh::KeyExpr(m.key("mount/telemetry/raw"))));
        }
        Impl* self = impl_.get();
        m.sub_traj.emplace(m.session->declare_subscriber(
            zenoh::KeyExpr(m.key("mount/trajectory")),
            [self](const zenoh::Sample& s) {
                try {
                    self->on_trajectory(s);
                } catch (...) {
                    std::lock_guard lock(self->mu);
                    self->reject_locked();
                }
            },
            zenoh::closures::none));
        m.q_cmd.emplace(m.session->declare_queryable(
            zenoh::KeyExpr(m.key("mount/cmd")), [self](const zenoh::Query& q) { self->on_query(q); },
            zenoh::closures::none));
    } catch (const zenoh::ZException& e) {
        stop();
        throw std::runtime_error(std::string("zenoh: ") + e.what());
    }
}

void ZenohBridge::stop() {
    Impl& m = *impl_;
    m.q_cmd.reset();
    m.sub_traj.reset();
    m.pub_raw.reset();
    m.pub_tel.reset();
    if (m.session) {
        try {
            m.session->close();
        } catch (const std::exception&) {
        }
        m.session.reset();
    }
}

std::size_t ZenohBridge::forward(Controller& controller) {
    Impl& m = *impl_;
    std::lock_guard lock(m.mu);
    std::size_t n = 0;
    while (!m.pending.empty()) {
        if (!controller.post(m.pending.front())) break;  // ring full: retry next time
        m.pending.pop_front();
        ++n;
    }
    return n;
}

void ZenohBridge::on_telemetry(const TelemetrySample& s, const LoopSnapshot& loop) {
    Impl& m = *impl_;
    {
        std::lock_guard lock(m.mu);
        m.last = s;
    }
    if (!m.session) return;

    std::uint64_t published = 0;
    if (!m.have_tel || s.cycle >= m.next_tel_cycle) {
        m.have_tel = true;
        m.next_tel_cycle = s.cycle + static_cast<std::uint64_t>(m.tel_every_cycles);
        m.pub_tel->put(zenoh::Bytes(encode_telemetry(s, m.opt.axis_names, &loop)));
        ++published;
    }
    if (m.pub_raw) {
        m.raw->add(s);
        if (m.raw->size() >= m.opt.raw_batch) {
            m.pub_raw->put(zenoh::Bytes(m.raw->take()));
            ++published;
        }
    }
    if (published) {
        std::lock_guard lock(m.mu);
        m.stats.published += published;
    }
}

BridgeStats ZenohBridge::stats() const {
    std::lock_guard lock(impl_->mu);
    return impl_->stats;
}

std::string ZenohBridge::key(const std::string& suffix) const { return impl_->key(suffix); }

}  // namespace maxmount::bridge
