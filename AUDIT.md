# Second independent audit verification

The audit reviewed source revision `ef20ce1`. Regression tests use synthetic
files and device state. The write harness exercises the writer, approval,
copy, readback, and cleanup without mounting or writing a real block device.

| Finding | Verification |
| --- | --- |
| B01 / F10 | `BuildWorkflow.test_failed_unmount_never_deletes_mounted_files_or_reports_success` exercises a failed unmount and checks that the simulated mounted payload survives. |
| B02 / F02 | `BuildWorkflow` changes the operation, disk connection, filesystem metadata and payload independently after the backup. Each test checks that the erase command is absent. |
| B03 / F02–F03 | `Approval.test_automated_pty_is_refused_even_with_correct_phrase` uses a real PTY. The CLI test verifies that the old option cannot reach a command. |
| B04 / F01 | `BuildWorkflow` tests partitions 3 and 4, an unrelated parent, and an extra partition. The successful workflow uses unrelated synthetic names for the disk and partitions. |
| B05 / F07–F08 / F16 | `ShareableIsAllowlist.test_populated_allowed_fields_and_nested_outputs_cannot_leak_text` checks both serialized public formats and confirms that current validation wins over stale state. `PrivateFileSafety` checks file permissions and symlinks. |
| A01–A02 | `Approval.test_recording_and_atomic_concurrent_consumption_stay_private` races independent approval handles and checks the journal permissions. |
| A03 / F06 / F15 | `DependencyCache` detects modified extracted code even with the correct archive marker, refuses extra modules and checks that a missing cache stays absent during a read-only check. |
| A04 / F14 | `DependencyCache.test_nullmoth_never_reuses_modified_staged_or_downloaded_payload` exercises the cache and staging functions. The writer tests cover the extra payload manifest and copy. |
| A05 / F12 | `Assessment.test_vendor_ids_alone_do_not_confer_native_support` checks Intel, AMD, and a synthetic HDA codec. |
| A06 / F11 | `BuildWorkflow.test_orchestrator_propagates_refused_dry_run` checks the stage result and the USB milestone. |
| A07 / F05 / F13 | `ChunklistVerification` uses a test signing key to exercise image verification, corruption after a valid signature, and version-specific cache reuse. |
| A08–A09 / F09 / F17 | `Compiler` uses executable fixtures to check symlink safety, a mismatched version and the highest-priority slot. |
| F04 | `DescriptionProbes` exercises whole-disk mounts by device number, holders on a non-prefix partition, and unavailable safety probes. |
| F18–F19 | Backups remain ignored. Documentation describes console approval, private reports and the limits of verification. The detailed case study was removed. |

Approval depends on the host's local console and Linux privilege model.
Unrestricted root can bypass these safeguards. Device checks inspect kernel
state and mount usage visible to the process. Offline regressions establish
behavior at the mocked boundaries. Booting, real media operations and
hardware compatibility require separate verification.

## Third independent audit

The third audit reviewed `d592e8f`. The following regressions exercise its
three remaining findings.

| Finding | Verification |
| --- | --- |
| T01 | `BuildWorkflow.test_added_staged_efi_file_refuses_before_erase` inserts a staged file after approval. Exact-path copies and full-tree readback are exercised by the successful workflow and by the unexpected on-media file test. |
| T02 | `BuildWorkflow` changes the config, inventory, ACPI and validator identity independently. A failing executable validator also checks that the writer passes the staged config before reaching the approval prompt. |
| T03 | `Assessment` tests exact PCI membership, an older unmatched device and an integrated GPU. The writer tests verify unsupported-device refusal, default exclusion and approved-payload inclusion on explicit opt-in. |

Inventory and EFI validation evidence live in a private work directory. The EFI
record binds the requested Darwin major, driver choice, complete EFI and
inventory manifests, and matching OpenCore archive and validator hashes.
Current run summaries describe only the stages they contain.
