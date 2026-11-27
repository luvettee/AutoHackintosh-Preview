# The NVIDIA path (NullMoth driver)

For GeForce cards from Turing (GTX 16xx / RTX 20xx) onwards, including every
RTX 30/40/50 card. macOS has no native driver for these; a third-party project
provides one, and it changes how installation has to work.

## Why this is a two-stage install

The driver is a set of macOS kernel extensions. They cannot exist before macOS
exists, so:

1. **Stage 1** — install macOS with no GPU driver. The display runs on the
   firmware's framebuffer (or, on some boards, not at all).
2. **Stage 2** — boot macOS with working network, install the driver, reboot.
   Only now is there acceleration.

Everything awkward about NVIDIA Hackintoshing follows from stage 1.

## What the driver gives you

- Metal 3, so the desktop compositor runs on the GPU
- OpenGL via Apple's GL-on-Metal, OpenCL, Core Image, Core ML
- Ray tracing and mesh shaders on supported cards

It is built on NVIDIA's own open kernel modules under macOS, so it works from
Turing onwards and nothing older. Old cards (Maxwell, Pascal) have no path here
at all.

## The boot-args the driver needs

The EFI generator applies these automatically when it finds a supported card.
They are recorded here so you can recognise them and explain them:

```
nvfb=1 nvaccel=1 nvfbheads=4 -nvkmsnosmooth
amfi_get_out_of_my_way=0x1 amfi=0x80 ipc_control_port_options=0
```

- `nvfb` / `nvaccel` switch on the framebuffer and accelerator.
- `nvfbheads=4` matches the card's display heads.
- `-nvkmsnosmooth` disables smooth boot handover (it hung on tested machines).
- The two AMFI arguments let WindowServer load a GPU bundle Apple did not sign.
  **This lowers system integrity protection and is a real security change.** Say
  so to the user; do not bury it.

`csr-active-config` is set to `430A0000` for the same reason: the driver's kexts
are unsigned. Flag this explicitly.

## The black-screen problem

Without acceleration, macOS's Setup Assistant may fail to draw: you get a black
screen with a cursor, or no signal after the verbose boot text.

`finish-setup.sh` (shipped on the recovery partition) creates the first account
from Recovery's Terminal and marks setup complete, so the *next* boot goes
straight to the desktop where the driver can be installed.

To use it: boot the stick, open Terminal from the Utilities menu, then

```sh
sh "/Volumes/Recovery HD/NullMoth/finish-setup.sh"
```

It only acts on an installed system, and does nothing if an account already
exists.

## Installing the driver

With macOS running and the desktop reachable (possibly over Screen Sharing or
VNC if there is no display signal):

```sh
cp "/Volumes/Recovery HD/NullMoth/nullmoth-nvidia-3.1.tar.gz" ~/
tar -xzf nullmoth-nvidia-3.1.tar.gz && cd pkgroot
shasum -a 256 -c SHA256SUMS
sudo ./install.sh
sudo shutdown -r now
```

Every line of the checksum file must say OK. Do not continue past a failure.

## After the driver is installed

Two things remain, both easy to get wrong:

1. **Block `IONDRVSupport`.** With the driver present, the firmware framebuffer
   competes for display index 0. Under `Kernel > Block` add
   `com.apple.iokit.IONDRVSupport`, Strategy `Exclude`, MinKernel `24.0.0`.
   The generator deliberately **omits** this on the installer stick: blocking it
   before the driver exists leaves no display driver at all.
2. **Verify** with
   `kmutil showloaded --list-only | grep nullmoth` (four kexts) and
   `system_profiler SPDisplaysDataType` (the card, with Metal: supported).

## Driver limitations

Check the driver release notes for supported GPU families and current
sleep and wake behavior. Verify graphics, USB port mapping and recovery
independently on the target.

## Licensing

The driver's kexts and plugin are under PolyForm Noncommercial 1.0.0: personal
and research use only. Do not use this path commercially.
