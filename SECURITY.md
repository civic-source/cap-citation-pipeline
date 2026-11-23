# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly:

1. **Do NOT open a public GitHub issue.**
2. Report privately via [GitHub Security Advisories](https://github.com/civic-source/cap-citation-pipeline/security/advisories/new).
3. Include a description of the vulnerability, steps to reproduce, and potential impact.

## Scope

Security reports relevant to this repository include:

- Unsafe YAML or external API payload parsing
- Path traversal or arbitrary file writes in annotation output generation
- Credential or secret exposure
- Dependency vulnerabilities with known exploits (`requirements.txt`)
- CI/CD pipeline or GitHub Actions injection vectors

## Response & Disclosure Timeline

- **Acknowledgment:** Within **48 hours** of receipt
- **Fix or Mitigation Plan:** Within **7 days** for critical issues
- **Public Disclosure:** Allow **90 days** for a fix and coordinated release before public disclosure

## Supported Versions

Only the latest release and the `main` branch are actively supported with security updates.
