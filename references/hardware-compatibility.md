# Hardware compatibility

How to read the `assess` stage, and what to tell the user.

The verdict buckets are: **natively supported**, **needs patches**,
**experimental driver**, **unsupported**. They describe *driver availability*,
not the likelihood the machine boots. A machine can be mostly native and still
fail to boot for firmware reasons.

## CPUs

| Family | Status | Notes |
| --- | --- | --- |
| Intel Sandy Bridge - Comet Lake | Works | iGPU acceleration native on most |
| Intel Alder Lake / Raptor Lake | Works with patches | Hybrid P/E cores need `CpuTopologyRebuild`; `ctrsmt` in boot-args; VT-d off recommended |
| Intel Arrow Lake (Core Ultra 200) | Works with patches | `CpuTopologyRebuild` must be **off**; `DevirtualiseMmio` on; `DisableIoMapper` on |
| AMD Zen / Zen 2+ | Works with patches | Per-core patches, kernel settings, `ProvideCurrentCpuInfo` |
| Anything with a locked/odd topology | Investigate | Look for a matching entry in 1401's `known_machines.json` before inventing quirks |

**F-series and KF-series Intel** have no integrated GPU. If the only graphics is
NVIDIA, there is no native acceleration at all; see the NVIDIA section.

## Graphics

| Vendor | Status | Notes |
| --- | --- | --- |
| AMD (RDNA 2/3, Polaris, Vega) | Native | The least troublesome choice |
| Intel iGPU | Native (with `WhateverGreen`) | Requires a CPU that has one |
| NVIDIA Kepler (GTX 6xx/7xx) | Legacy native | Dropped from macOS after Big Sur; OCLP can restore |
| Discrete NVIDIA device listed in the pinned NullMoth firmware table | **Experimental** | Explicit opt-in. The optional driver is installed after boot |

An NVIDIA-only machine is the most likely to disappoint. Set the expectation
before work starts: acceleration arrives only after macOS is installed and the
driver is run, and it is third-party software.

## Network

| Chip | Status |
| --- | --- |
| Intel Ethernet | `IntelMausi` |
| Realtek RTL8111/8168 | `RealtekRTL8111` |
| Broadcom (BCM94360 etc.) | Native; the only truly painless Wi-Fi |
| Intel Wi-Fi | `itlwm`/`AirportItlwm`, version-sensitive |
| Realtek/MediaTek Wi-Fi | Mostly unsupported; a USB dongle is easier |
| Any USB Ethernet adapter | Usually works |

Do not promise Wi-Fi. Ethernet is the reliable path.

## Audio

Realtek codecs are covered by `AppleALC` with a layout-id chosen from the codec
id. HDMI/DisplayPort audio routed through an NVIDIA card is **not** supported by
the NullMoth driver; tell the user to use the motherboard jacks or a USB DAC.

## Storage

NVMe is supported. SATA is supported. **Intel VMD / RAID mode** hides disks from
macOS entirely: the disk must be switched to AHCI, which can break an existing
Windows install. That is a conversation to have before, not after.

## What "unsupported" means in practice

It means no native driver and no maintained third-party one. Options, in order
of honesty:

1. Replace the part (a supported Wi-Fi card or AMD GPU is cheap relative to the
   time spent).
2. Accept it does not work.
3. Experiment, labelling it clearly as unverified.

Do not present a plausible-looking configuration for unsupported hardware as if
it were tested.
