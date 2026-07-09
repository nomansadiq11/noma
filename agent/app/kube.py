from __future__ import annotations

import os

from kubernetes import client, config


def is_true(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def apply_kube_overrides() -> None:
    # For local Docker + kind, kubeconfig server usually points to 127.0.0.1 on the host.
    # From inside a container that address is not reachable, so allow an override.
    override_server = os.getenv("KUBE_API_SERVER")
    skip_tls_verify = is_true(os.getenv("KUBE_SKIP_TLS_VERIFY"))

    if not override_server and not skip_tls_verify:
        return

    cfg = client.Configuration.get_default_copy()
    if override_server:
        cfg.host = override_server
    if skip_tls_verify:
        cfg.verify_ssl = False
        cfg.assert_hostname = False
    client.Configuration.set_default(cfg)


def load_kube_context() -> None:
    kubeconfig = os.getenv("KUBECONFIG")
    context = os.getenv("KUBE_CONTEXT")
    if kubeconfig:
        config.load_kube_config(config_file=kubeconfig, context=context)
    else:
        config.load_incluster_config()
    apply_kube_overrides()
