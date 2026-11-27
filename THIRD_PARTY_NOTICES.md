# Third-party notices

AutoHackintosh orchestrates other projects. This file records what is used, how,
and under which licence. It is not legal advice.

**None of these projects are affiliated with AutoHackintosh.** Bugs in this tool
are not their bugs.

---

## OpenCore (OpenCorePkg)

- **Used for:** the bootloader binaries written to the EFI partition, and
  `ocvalidate`, the configuration validator.
- **Licence:** BSD-3-Clause
- **Source:** https://github.com/acidanthera/OpenCorePkg
- **How:** the official release archive is downloaded, verified against
  OpenCore's published `SHA256SUMS.txt` where available, and extracted. No
  modification. `ocvalidate` is used unmodified and its output is reproduced
  verbatim in build reports.
- **Note:** `ocvalidate` only validates configurations for its own release. The
  pinned version in `scripts/fetch_deps.py` must match the OpenCore used.

## Dortania OpenCore Install Guide

- **Used for:** compatibility knowledge and troubleshooting reference.
- **Licence:** documentation, reproduced only as paraphrased guidance.
- **Source:** https://dortania.github.io/OpenCore-Install-Guide/
- **How:** consulted, cited by name in references, never bundled.

## OpCore-Simplify

- **Used for:** generating `config.plist`, SSDTs and kext selection.
- **Licence:** BSD-3-Clause
- **Source:** https://github.com/lzhoang2801/OpCore-Simplify
- **How:** AutoHackintosh uses the fork **vendored inside 1401**, not upstream,
  because 1401's engine depends on fixes made there. Its pinned commit
  (`814061d40d6292d7c656c90e7cc715a250db89b6`) is recorded in
  `references/upstream-pins.json`. Used unmodified.

## Hardware Sniffer

- **Used for:** hardware inventory and ACPI table dumps.
- **Licence:** BSD-3-Clause
- **Source:** https://github.com/lzhoang2801/Hardware-Sniffer
- **How:** the copy vendored inside 1401 (commit
  `6570f91c33c63f5bf481db31789f903b0d12099f`) is executed with its own Linux
  backend. Output is post-processed by `scripts/report_normalize.py`; those edits
  are recorded in the diagnostic report.

## 1401

- **Used for:** the compatibility rule engine, policy pass, and the NVIDIA driver
  packaging.
- **Licence:** **PolyForm Noncommercial 1.0.0** — see below.
- **Source:** https://github.com/nullmoth/1401
- **How:** its Windows build is downloaded, its embedded engine is run headless
  on Linux, and small runtime compatibility repairs are applied by
  `scripts/linuxcompat.py`. **No source file is edited on disk**; the repairs are
  in-process wrappers applied at import time, and every one is listed in the
  diagnostic report so a maintainer can see exactly what differed.

> **Non-commercial:** 1401 is licensed for personal, research, educational and
> non-profit use. Commercial use is not permitted by its licence. Because
> AutoHackintosh depends on it, AutoHackintosh as a whole is effectively
> restricted to non-commercial use, despite this project's own code being MIT.

## NullMoth NVIDIA driver

- **Used for:** Metal acceleration on NVIDIA Turing-and-newer GPUs. **Optional**
  — only staged when such a card is present.
- **Licence:** mixed and **non-commercial**:
  - `plugin/`, `kexts/`, `build/`, `package/`: PolyForm Noncommercial 1.0.0
  - `translator/`: LGPL-3.0-or-later (based on `metal2vulkan`)
  - `nvk/`: MIT (Mesa changes)
  - GSP firmware: NVIDIA's redistributable firmware licence
- **Source:** https://github.com/nullmoth/nvidia-macos-driver
- **How:** the release archive is downloaded, verified against the project's
  published `SHA256SUMS.txt`, and copied to the recovery partition for the user
  to install from within macOS. Its own `install.sh` is run by the user as
  documented upstream. AutoHackintosh does not claim any guarantee about its
  behaviour.

## ACPICA

- **Used for:** `iasl`, the ACPI disassembler OpCore-Simplify requires.
- **Licence:** BSD-3-Clause OR GPL-2.0 (dual)
- **Source:** https://github.com/acpica/acpica
- **How:** the pinned release (20260408) is compiled from source with `make
  iasl` and installed into the OpCore-Simplify `Scripts/` directory. Reasons for
  pinning this exact version are in `references/linux-engine-notes.md`.

## Kexts referenced in generated EFIs

Generated configurations may reference, and `fetch_deps`/the engine may
download:

| Kext | Author | Licence |
| --- | --- | --- |
| Lilu | Acidanthera | BSD-3-Clause |
| VirtualSMC, SMCProcessor, SMCSuperIO | Acidanthera | BSD-3-Clause |
| WhateverGreen | Acidanthera | BSD-3-Clause |
| AppleALC | Acidanthera | BSD-3-Clause |
| NVMeFix | Acidanthera | BSD-3-Clause |
| AMFIPass | dortania | BSD-3-Clause |
| RestrictEvents | Acidanthera | BSD-3-Clause |
| CpuTopologyRebuild | b00t0x | BSD-3-Clause |
| RealtekRTL8111 | Mieze | GPL-2.0 |
| USBToolBox / UTBMap | USBToolBox project | MIT |
| XHCI-unsupported | Acidanthera | BSD-3-Clause |

Each kext keeps its own licence and its own `Info.plist` authorship. AutoHackintosh
does not modify kexts.

## Apple software

macOS recovery images are Apple's property. `scripts/fetch_recovery.py` requests
them from Apple's own servers (`osrecovery.apple.com`) on the user's behalf and
does not bundle, mirror or redistribute them. Installing macOS on non-Apple
hardware is restricted by Apple's licence; users accept that constraint
themselves.

## Tooling used to build the stick

`sgdisk` (GNU GPL-2.0), `mkfs.vfat` (dosfstools, GPL-2.0), `mkfs.hfsplus`
(hfsprogs, GPL-2.0). Invoked as normal command-line tools.

---

If you believe something here is attributed incorrectly, please open an issue.
