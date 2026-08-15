# MP32 Control — Product Metadata

## Application identity

- Product name: **MP32 Control**
- Version: **1.3.1**
- Bundle identifier: `com.studio.mp32control`
- Category: Audio hardware remote control / studio utility
- Platforms: macOS, Windows, iPhone/iPad web app (PWA), other modern browsers

## Short description

Independent network control panel for the Antelope Audio MP32 32-channel microphone
preamplifier.

## About

MP32 Control is an independent remote control application for the Antelope Audio MP32
32-channel microphone preamplifier. It discovers the MP32 on the local network and
controls gain, 48 V phantom power, input type, presets and live VU metering.

Channel names, colours, groups, stereo links and Public Notes can be shared between
desktop and mobile controllers on the same LAN. Local Notes remain private to the
browser in which they were created. The application runs natively on macOS and Windows
and also serves a phone/tablet web interface.

MP32 Control communicates directly with compatible hardware over the local network.

## Disclaimer

This is an independent project and is not affiliated with or endorsed by Antelope Audio.
Antelope Audio and MP32 may be trademarks of their respective owner.

## Credit

**Built by Franck Reisner, fueled by caffeine.**

## Signing and trust

The macOS build is ad-hoc signed by default, which seals the application but does not
establish a publicly trusted publisher identity. A trusted macOS release requires an
Apple Developer ID Application certificate and should be notarized by Apple.

The Windows build includes icon and VersionInfo metadata. A trusted Windows release
requires a publisher code-signing certificate installed on the build machine. The build
scripts support both certificate paths when those credentials are available.
