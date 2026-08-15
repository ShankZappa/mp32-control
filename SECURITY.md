# Security

MP32 Control is designed for a trusted private local network. It does not currently
authenticate HTTP clients, encrypt device-control traffic, or provide internet-facing
access controls.

- Do not forward TCP port 8765 from a router.
- Do not run the controller on untrusted public Wi-Fi.
- Treat preset JSON files as untrusted input and review their source.
- Keep signing certificates, network logs, serial numbers, and private network details out
  of issues and commits.

To report a security issue, contact the repository owner privately rather than opening a
public issue containing exploit details or private infrastructure information.
