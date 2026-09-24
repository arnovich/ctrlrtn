# Security policy

ctrlrtn is a proxy that **forwards client API keys** and **records full
request/response bodies** — its threat model and hardening guidance live in
[docs/deploy.md](docs/deploy.md). Read that before deploying; the short
version: never expose the router publicly (the `/ctrlrtn/` control plane is
unauthenticated by design and assumes localhost / a trusted private network).

## Supported versions

The latest release (and `main`) only.

## Reporting a vulnerability

Please report privately via GitHub:
**Security → Report a vulnerability** on this repository — not in a public
issue. You should hear back within a few days (solo maintainer).

In scope, especially:
- any path by which a client credential is **persisted** (the contract is:
  forwarded, then redacted from the recorded trace at capture time);
- escapes of the control-plane trust assumptions beyond what
  `docs/deploy.md` documents (outcome poisoning / corpus pollution on a
  loopback-bound router is documented, not news);
- request smuggling / SSRF through the proxy;
- anything that lets recorded traffic leave the box.

Local-model training is not currently enabled. The reviewed future contract in
[`docs/local-model-training.md`](docs/local-model-training.md) treats corpus
materialization and trainer execution as new sensitive boundaries: no generic
shell jobs, inherited credentials, evaluation-split materialization, or
automatic serving. A report that an implemented future adapter violates those
boundaries is in scope.
