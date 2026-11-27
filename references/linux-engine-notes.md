# Running 1401's engine on Linux

Notes for anyone modifying the scripts. Each entry is a problem that cost real
debugging time, recorded so it is solved once.

## 1. The engine is portable, the apps are not

1401 publishes a macOS `.dmg` and a Windows `.zip`. The Windows zip embeds a
full Python 3.13 **and the entire application as plain Python** under
`1401/engine/app/`:

```
engine/app/p1401/                 # the rule engine (policy, efi_rules, nullmoth)
engine/app/upstream/Hardware-Sniffer/    # includes Scripts/platforms/linux.py
engine/app/upstream/OpCore-Simplify/     # 1401's patched fork
```

`p1401.engine.build(report, acpi_dir, out_dir, policy=engine.Policy(...))` runs
unmodified. There is no need for Wine.

Use 1401's **vendored** OpCore-Simplify, not upstream: its `PINNED.json` records
commit `814061d` and the engine depends on fixes made there.

## 2. Hardware Sniffer has a Linux backend

`Hardware-Sniffer-CLI.py` accepts Linux explicitly and imports
`Scripts/platforms/linux.py`. Run it as root, because it reads
`/sys/firmware/acpi/tables` and DMI.

It writes a genuine `Report.json` plus an `ACPI/` dump — the same inputs the
engine expects from Windows.

## 3. Two schema mismatches only Linux produces

The validator was written against the Windows backend and rejects the **whole
report** over one cosmetic value. `scripts/report_normalize.py` repairs both:

| Field | Linux emits | Schema wants | Why dropping/mapping is safe |
| --- | --- | --- | --- |
| `System Devices.*.ACPI Path` | `\_TZ_.FAN3`, `\ALCW` | `^[\\]?_SB(\.[A-Z0-9_]+)+$` | That section is read only via `Device ID`/`Bus Type`/`Device`; `ACPI Path` there feeds no decision |
| `Monitor.*.Connector Type` | `HDMI-A`, `DP-1` | one of VGA/DVI/HDMI/LVDS/DP/eDP/Internal/Uninitialized | Only compared against `"VGA"` and `("DVI","HDMI")`; mapping is exact |

Anything unrecognised is dropped, never guessed, and every edit is recorded in
the diagnostic report.

## 4. iasl must be built, not borrowed

OpCore-Simplify looks for `iasl-dev`, `iasl-stable`, `iasl` in `Scripts/`. The
shipped copies are Mach-O (macOS) and PE (Windows). Neither runs on Linux.

Do not simply use the distro package: 1401 pins **ACPICA 20260408** and
documents in `p1401/iasl_pin.py` that 20251212 *segfaults* on some real DSDTs.
`iasl_provision.py` compiles the pinned release (~17 s) and installs it as
`iasl-dev`. Provisioning fails if this compiler cannot be built and verified.

## 5. `Response` is a stream, not bytes

`p1401/mirror.py` defines `Response(io.BytesIO)`. `mirror.fetch` returns one.
Callers doing `len(data)` or `hashlib.sha256(data)` raise
`TypeError: object of type 'Response' has no len()`.

**The damage is not the exception.** Each site sits inside a bare
`except Exception` whose purpose is "an optional dependency must not break a
build", so the build reports success while skipping work. Observed:

- `own_virtualsmc` — the NullMoth VirtualSMC sensor build silently skipped
- `own_experimental` — experimental driver staging silently skipped
- `community` — community UEFI drivers silently skipped

`scripts/linuxcompat.py` wraps the consumers. Three hard-won details:

1. **Do not wrap `make_request` globally.** Tried first; it breaks
   `p1401/apple.py`, which reads `.headers`/`.getcode()`/`.info()` for the
   recovery download (`'bytes' object has no attribute 'getcode'`).
2. **Rewrite the original function's `__defaults__`.** `own_virtualsmc.replace`
   does `data = fetch(url)` on its own default, so wrapping only the outer call
   leaves the inner one unguarded. That was the bug that survived the first fix.
3. **Never compute the default's slot from the parameter index.**
   `place(out_dir, notices, nvidia=False, fetch=...)` — index arithmetic moved
   `mirror.fetch` into `nvidia` and produced
   `expected str, bytes or os.PathLike object, not function`. Rewrite defaults by
   *type* (the only callable default is `fetch`) instead.

All three are covered by `tests/test_linuxcompat.py`.

## 6. Recovery media is buildable from Linux

`fetch_recovery.py` implements Apple's protocol exactly as `macrecovery.py`
documents it:

- **A session cookie is mandatory.** `GET https://osrecovery.apple.com/` first;
  a POST without it returns HTTP 401.
- **Fields are product/session records, not URLs.** `AP` = product id,
  `AU`/`AT` = image URL + token, `CU`/`CT` = signed-chunklist URL + token.
  `AH`/`CH` are the respective hashes. A wrong interpretation treats `AH` as a
  URL; that was a real bug that produced a corrupt-by-design image path.
- **The chunklist is binary *and* signed.** Header `'<4sIBBBxQQQ'` (magic
  `CNKL`), `'<I32s'` chunk records, then a 256-byte RSA signature that must
  verify against Apple's EFI ROM public key. Signature method 2 is unsigned and
  is refused. A text parser sees nothing; a missing signature is not proof.
- **Only verified images are kept.** The image is downloaded to a temp file,
  verified, then published atomically. A corrupt cache entry is deleted, not
  reused.

The image wraps an **HFS+** volume, so partition 2 must be HFS+ (`mkfs.hfsplus`),
not APFS. APFS is for the installed system, not the recovery partition.

## 7. Reading a built config

Useful assertions for regression testing a generated EFI:

```
NVRAM.Add.7C436110-.../boot-args        includes nvfb=1 when NVIDIA is present
NVRAM.Add.7C436110-.../csr-active-config 430A0000 for the NVIDIA driver
Booter.Quirks.ResizeAppleGpuBars        0 for Resizable BAR cards
UEFI.Quirks.ResizeGpuBars               -1
Misc.Security.SecureBootModel           Disabled
Kernel.Block                            IONDRVSupport excluded only AFTER driver install
```

A rebuild from a clean workdir should reproduce an earlier verified config
exactly for the same hardware and macOS. That equality is the strongest
regression test available without physical hardware.

## 8. Interrupted runs

Stages are idempotent and read their inputs from `${WORKDIR}`. Re-running
`autohackintosh.py <stage>` resumes without redownloading verified artifacts
(`manifest.json`). The engine refuses to build into a non-empty directory it does
not own, so a stale `efi/` must be removed deliberately rather than silently
overwritten.
