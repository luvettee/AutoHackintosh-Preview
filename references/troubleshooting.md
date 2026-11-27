# Troubleshooting

Read the failure signature, not the symptom. Each entry says where the evidence
is, what it means, and what to change.

## Where the evidence is

| Evidence | Location |
| --- | --- |
| Stage failure | the stage's JSON `error` in `last-run.json` |
| OpenCore log | `EFI/OC/opencore-*.txt` on the EFI partition of the stick |
| macOS panic | `EFI/OC/panic-*.txt`, or `/Library/Logs/DiagnosticReports` after boot |
| Screen text | ask the user for the **exact last line** |
| Validation | `ocvalidate` output recorded in the `efi` stage |

The EFI is configured with file logging on purpose (`Misc > Debug > Target 67`,
`AppleDebug`, `ApplePanic`). If there is no log, the stick never reached
OpenCore.

## OpenCore stops before the picker

| Symptom | Cause | Fix |
| --- | --- | --- |
| Firmware ignores the stick | CSM on, or non-UEFI entry chosen | Turn CSM off; pick the UEFI: entry |
| Picker shows, no macOS entry | Recovery folder missing or wrong filesystem | Partition 2 must be HFS+ with `com.apple.recovery.boot/BaseSystem.dmg` |
| Black screen after picking | No display driver yet | Expected on NVIDIA; see `nvidia-nullmoth.md` |

## Stops at a named stage

| Last line on screen | Meaning | Action |
| --- | --- | --- |
| `EXITBS:START` | Booter hand-off failed | Rebuild with `DevirtualiseMmio` toggled; on hybrid Intel also try disabling Hyper-Threading in BIOS |
| `[EB|#LOG:EXITBS:START]` then reset | Same, with logging on | As above; check the log for the last MMIO entry |
| Stuck at `PCI configuration end` | Resizable BAR / GPU bring-up | `ResizeAppleGpuBars=0`; verify ReBAR setting matches |
| `Still waiting for root device` | Storage not visible | Check SATA mode is AHCI, VMD off |
| Panic in `_pmaInitialize` / `NVRM::go` | NVIDIA driver mis-initialising | Wrong driver version for the card; try the newest |
| Prohibited sign | SMBIOS/secure boot mismatch | `SecureBootModel` must be `Disabled` |

## Boots, then something is wrong

| Symptom | Likely cause | Check |
| --- | --- | --- |
| No network | Kext missing or wrong chip | `system_profiler SPNetworkDataType`; confirm kext loaded |
| No audio | Wrong `layout-id` | Try other layouts for that codec |
| USB ports dead | Port map does not match | Remap with USBToolBox; the default map is generic |
| Sleep breaks the display | Known NVIDIA driver caveat | Documented limitation; sleep may not be usable |
| Random freezes | Experimental driver | Correlate with GPU load; report it, do not paper over it |
| Graphics stutter | Same | Same |

## Build-side failures (on Linux)

| Error | Cause | Fix |
| --- | --- | --- |
| `hardware report rejected: ... does not match pattern` | Linux sniffer emits a value the schema rejects | `report_normalize.py` should have handled it; if a new field appears, add a rule there and a test |
| `ACPI disassembler exit 1` | No working `iasl` | Run the `deps` stage to provision the compiler again |
| `TypeError: object of type 'Response' has no len()` | Known 1401-on-Linux bug | `linuxcompat.apply()` must run before any build |
| `EFI failed validation` | ocvalidate rejected the config | Read its message; it names the key |
| `Could not download ...` | Network or mirror | Retry; check `manifest.json` for what did arrive |
| `confirmation requires a foreground Linux virtual console` | Approval must be entered on the host console | Switch to a local virtual console and run the command there |
| `device state, plan or payload changed since approval` | The approved inputs changed before erasure | Review the current plan and approve it again |
| `mount cleanup failed` | A volume could not be unmounted or its empty directory could not be removed | Resolve the reported mount failure before reusing the stick |

## When you are stuck

1. Reproduce once and capture the evidence above.
2. Read the upstream source for the failing path rather than guessing.
3. Make one change, rebuild, compare.
4. Record what you learned in `references/linux-engine-notes.md` if it is general.

Do not open an upstream issue with an AI-generated hypothesis presented as a
bug report. If something looks like a genuine upstream defect, reproduce it
minimally first and show the code path.
