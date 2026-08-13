# Cloud-init deployment

Paste [`cloudmoo.yaml`](cloudmoo.yaml) into the user-data/cloud-init field
when creating a fresh Ubuntu or Debian VM on DigitalOcean, Hetzner, Linode,
Vultr, UpCloud, AWS, or another provider. It downloads the repository
installer from GitHub and starts the complete Docker Compose stack.

The default user data discovers the VM's public IPv4 address. For a DNS name,
replace the final command in the YAML with:

```yaml
- [bash, /tmp/cloudmoo-install.sh, --domain, monitors.example.com]
```

The installer is an HTTP bootstrap on port 8000. Add TLS and firewall rules
before treating the VM as a public production endpoint. For reproducible
installs, replace `main` in the download URL and add `--branch <release-tag>`
to the installer command.
