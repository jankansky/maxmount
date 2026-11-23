// SPDX-License-Identifier: GPL-3.0-or-later
// SOEM 2.x backend. Start-up sequence:
//   ecx_init -> ecx_config_init (scan) -> identity checks -> ecx_configdc
//   -> ecx_config_map_group, which calls our PRE-OP->SAFE-OP hook per drive (startup SDOs,
//      PDO mapping, DC SYNC0) and then requests SAFE-OP
//   -> check process-image sizes against the descriptors -> OP
// Cyclic: receive() reads the frame sent by the previous send(); DC time from that frame
// drives a PI loop that phase-locks the host cycle to SYNC0.
#include "maxmount/fieldbus/soem_fieldbus.hpp"

#include <array>
#include <cinttypes>
#include <cstdio>
#include <cstring>

extern "C" {
#include "soem/soem.h"
}

namespace maxmount {

namespace {
constexpr std::size_t kIoMapSize = 4096;

std::string hex32(std::uint32_t v) {
    char buf[16];
    std::snprintf(buf, sizeof(buf), "0x%08" PRIX32, v);
    return buf;
}
}  // namespace

struct SoemFieldbus::Impl {
    explicit Impl(SoemConfig c) : cfg(std::move(c)) {
        for (const auto& d : cfg.drives) codecs.emplace_back(d.descriptor);
        inputs.resize(cfg.drives.size());
        outputs.resize(cfg.drives.size());
    }

    SoemConfig cfg;
    ecx_contextt ctx{};
    alignas(8) std::array<std::uint8_t, kIoMapSize> iomap{};
    std::vector<PdoCodec> codecs;
    std::vector<Cia402Inputs> inputs;
    std::vector<Cia402Outputs> outputs;
    FieldbusStats stats{};
    bool opened = false;
    bool frame_pending = false;
    std::int64_t dc_integral = 0;
    nanos correction = 0;
    std::string error;

    std::uint16_t slave_of(std::size_t drive) const {
        return static_cast<std::uint16_t>(cfg.drives[drive].bus_position + 1);  // SOEM is 1-based
    }

    const SoemDriveConfig* drive_at_slave(std::uint16_t slave) const {
        for (const auto& d : cfg.drives) {
            if (d.bus_position + 1 == slave) return &d;
        }
        return nullptr;
    }

    bool sdo_write(std::uint16_t slave, const SdoWrite& w) {
        std::uint8_t buf[4] = {};
        const auto size = static_cast<int>(size_of(w.type));
        write_le(buf, static_cast<std::uint64_t>(w.value), static_cast<std::size_t>(size));
        const int wkc = ecx_SDOwrite(&ctx, slave, w.index, w.subindex, FALSE, size, buf, EC_TIMEOUTRXM);
        if (wkc <= 0) {
            char msg[96];
            std::snprintf(msg, sizeof(msg), "slave %u: SDO write 0x%04X:%02X failed; ", slave, w.index,
                          w.subindex);
            error += msg;
            return false;
        }
        return true;
    }

    // Runs inside ecx_config_map_group, before the slave is requested to SAFE-OP.
    void configure_slave(std::uint16_t slave) {
        const SoemDriveConfig* d = drive_at_slave(slave);
        if (!d) return;
        for (const auto& w : d->descriptor.startup_sdo) {
            if (!sdo_write(slave, w)) return;
        }
        for (const auto& w : pdo_mapping_sdos(d->descriptor)) {
            if (!sdo_write(slave, w)) return;
        }
        if (d->descriptor.distributed_clocks) {
            ecx_dcsync0(&ctx, slave, TRUE, static_cast<uint32>(cfg.cycle),
                        static_cast<int32>(cfg.sync0_shift));
        }
    }

    static int po2so_hook(ecx_contextt* context, uint16 slave) {
        static_cast<Impl*>(context->userdata)->configure_slave(slave);
        return 1;
    }

    void roundtrip() {
        ecx_send_processdata(&ctx);
        ecx_receive_processdata(&ctx, EC_TIMEOUTRET);
    }

    void describe_states() {
        ecx_readstate(&ctx);
        for (int i = 1; i <= ctx.slavecount; ++i) {
            const ec_slavet& s = ctx.slavelist[i];
            char msg[160];
            std::snprintf(msg, sizeof(msg), "slave %d (%s): state 0x%02X AL 0x%04X %s; ", i, s.name,
                          s.state, s.ALstatuscode, ec_ALstatuscode2string(s.ALstatuscode));
            error += msg;
        }
    }

    // PI loop from SOEM's ec_sample: keep frame transmission master_sync_offset after SYNC0.
    void dc_sync(std::int64_t reftime) {
        const std::int64_t cycle = cfg.cycle;
        std::int64_t delta = (reftime - cfg.master_sync_offset) % cycle;
        if (delta > cycle / 2) delta -= cycle;
        const std::int64_t err = -delta;
        dc_integral += err;
        correction = static_cast<nanos>(static_cast<double>(err) * 0.01 +
                                        static_cast<double>(dc_integral) * 0.00002);
    }

    bool start() {
        error.clear();
        for (std::size_t i = 0; i < cfg.drives.size(); ++i) {
            if (const std::string e = validate(cfg.drives[i].descriptor); !e.empty()) {
                error = "drive " + std::to_string(i) + ": " + e;
                return false;
            }
            if (codecs[i].output_size() == 0 || codecs[i].input_size() == 0) {
                error = "drive " + std::to_string(i) + ": descriptor maps no process data";
                return false;
            }
        }

        std::memset(&ctx, 0, sizeof(ctx));  // large C struct: avoid a stack temporary
        ctx.userdata = this;
        if (!ecx_init(&ctx, cfg.interface.c_str())) {
            error = "cannot open " + cfg.interface + " (needs root or CAP_NET_RAW)";
            return false;
        }
        opened = true;

        if (ecx_config_init(&ctx) <= 0) {
            error = "no EtherCAT slaves found on " + cfg.interface;
            return false;
        }

        bool any_dc = false;
        for (std::size_t i = 0; i < cfg.drives.size(); ++i) {
            const std::uint16_t slave = slave_of(i);
            if (slave > ctx.slavecount) {
                error = "drive " + std::to_string(i) + ": bus position " +
                        std::to_string(cfg.drives[i].bus_position) + " but only " +
                        std::to_string(ctx.slavecount) + " slaves found";
                return false;
            }
            ec_slavet& s = ctx.slavelist[slave];
            const DriveDescriptor& d = cfg.drives[i].descriptor;
            if (d.vendor_id && *d.vendor_id != s.eep_man) {
                error = "slave " + std::to_string(slave) + " (" + s.name + "): vendor id " +
                        hex32(s.eep_man) + " does not match descriptor " + hex32(*d.vendor_id);
                return false;
            }
            if (d.product_code && *d.product_code != s.eep_id) {
                error = "slave " + std::to_string(slave) + " (" + s.name + "): product code " +
                        hex32(s.eep_id) + " does not match descriptor " + hex32(*d.product_code);
                return false;
            }
            s.PO2SOconfig = &Impl::po2so_hook;
            any_dc = any_dc || d.distributed_clocks;
        }

        if (any_dc && !ecx_configdc(&ctx)) {
            error = "distributed clocks requested but no DC-capable slave found";
            return false;
        }

        ecx_config_map_group(&ctx, iomap.data(), 0);
        const ec_groupt& grp = ctx.grouplist[0];
        if (grp.Obytes + grp.Ibytes > kIoMapSize) {
            error = "process image exceeds the I/O map";
            return false;
        }
        if (!error.empty()) return false;  // an SDO write in the hook failed

        for (std::size_t i = 0; i < cfg.drives.size(); ++i) {
            const ec_slavet& s = ctx.slavelist[slave_of(i)];
            if (s.Obytes != codecs[i].output_size() || s.Ibytes != codecs[i].input_size()) {
                error = "slave " + std::to_string(slave_of(i)) + ": process image is " +
                        std::to_string(s.Obytes) + " out / " + std::to_string(s.Ibytes) +
                        " in bytes, descriptor maps " + std::to_string(codecs[i].output_size()) +
                        " / " + std::to_string(codecs[i].input_size());
                return false;
            }
        }

        if (ecx_statecheck(&ctx, 0, EC_STATE_SAFE_OP, EC_TIMEOUTSTATE * 4) != EC_STATE_SAFE_OP) {
            error = "not all slaves reached SAFE-OP: ";
            describe_states();
            return false;
        }
        stats.expected_working_counter = grp.outputsWKC * 2 + grp.inputsWKC;

        // Outputs are all zero here: controlword 0 keeps every drive disabled.
        for (std::size_t i = 0; i < cfg.drives.size(); ++i) {
            outputs[i] = Cia402Outputs{};
            codecs[i].encode(outputs[i], ctx.slavelist[slave_of(i)].outputs);
        }
        roundtrip();

        ctx.slavelist[0].state = EC_STATE_OPERATIONAL;
        ecx_writestate(&ctx, 0);
        for (int attempt = 0; attempt < 200; ++attempt) {
            roundtrip();
            if (ecx_statecheck(&ctx, 0, EC_STATE_OPERATIONAL, 10'000) == EC_STATE_OPERATIONAL) {
                frame_pending = false;
                return true;
            }
        }
        error = "not all slaves reached OP: ";
        describe_states();
        return false;
    }

    void stop() {
        if (!opened) return;
        ctx.slavelist[0].state = EC_STATE_INIT;
        ecx_writestate(&ctx, 0);
        ecx_close(&ctx);
        opened = false;
        frame_pending = false;
    }

    bool receive() {
        if (!frame_pending) return false;
        frame_pending = false;
        const int wkc = ecx_receive_processdata(&ctx, cfg.receive_timeout_us);
        stats.last_working_counter = wkc;
        if (wkc <= 0) {
            ++stats.lost_frames;
            return false;
        }
        for (std::size_t i = 0; i < codecs.size(); ++i) {
            codecs[i].decode(ctx.slavelist[slave_of(i)].inputs, inputs[i]);
        }
        if (ctx.slavelist[0].hasdc) dc_sync(ctx.DCtime);
        if (wkc < stats.expected_working_counter) {
            ++stats.working_counter_errors;
            return false;
        }
        return true;
    }

    bool send() {
        if (!opened) return false;
        for (std::size_t i = 0; i < codecs.size(); ++i) {
            codecs[i].encode(outputs[i], ctx.slavelist[slave_of(i)].outputs);
        }
        ecx_send_processdata(&ctx);
        frame_pending = true;
        ++stats.cycles;
        return true;
    }
};

SoemFieldbus::SoemFieldbus(SoemConfig cfg) : impl_(std::make_unique<Impl>(std::move(cfg))) {}
SoemFieldbus::~SoemFieldbus() { impl_->stop(); }

bool SoemFieldbus::start() { return impl_->start(); }
void SoemFieldbus::stop() { impl_->stop(); }
std::size_t SoemFieldbus::drive_count() const { return impl_->cfg.drives.size(); }
bool SoemFieldbus::receive() { return impl_->receive(); }
bool SoemFieldbus::send() { return impl_->send(); }
const Cia402Inputs& SoemFieldbus::inputs(std::size_t drive) const { return impl_->inputs[drive]; }
Cia402Outputs& SoemFieldbus::outputs(std::size_t drive) { return impl_->outputs[drive]; }
FieldbusStats SoemFieldbus::stats() const { return impl_->stats; }
nanos SoemFieldbus::cycle_correction_ns() const { return impl_->correction; }
const std::string& SoemFieldbus::last_error() const { return impl_->error; }

}  // namespace maxmount
