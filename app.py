from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import paramiko
import streamlit as st

st.set_page_config(page_title="SSH Remote Console", page_icon=":terminal:", layout="wide")

CONFIG_PATH = Path(os.environ.get("SSH_GUI_CONFIG", Path(__file__).with_name("config.json")))
DEFAULT_PORT = 22
DEFAULT_TIMEOUT = 10


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        st.error(f"File di configurazione non trovato: `{CONFIG_PATH}`")
        st.stop()
    except json.JSONDecodeError as e:
        st.error(f"Configurazione JSON non valida: {e}")
        st.stop()

    hosts = cfg.get("hosts")
    if not isinstance(hosts, list) or not hosts:
        st.error("La configurazione deve contenere una lista non vuota 'hosts'.")
        st.stop()
    return cfg


def run_ssh(host_cfg: dict, command: str, password: str | None) -> dict:
    """Esegue `command` via SSH su `host_cfg` e ritorna il risultato."""
    start = time.time()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    result = {"ok": False, "code": None, "out": "", "err": "", "elapsed": 0.0}
    try:
        kwargs: dict = {
            "hostname": host_cfg["host"],
            "port": int(host_cfg.get("port", DEFAULT_PORT)),
            "username": host_cfg.get("user"),
            "timeout": int(host_cfg.get("timeout", DEFAULT_TIMEOUT)),
        }
        key_file = host_cfg.get("key_file")
        if key_file:
            kwargs["key_filename"] = os.path.expanduser(key_file)
        if password:
            kwargs["password"] = password
        client.connect(**kwargs)
        _stdin, stdout, stderr = client.exec_command(command)
        result["out"] = stdout.read().decode(errors="replace")
        result["err"] = stderr.read().decode(errors="replace")
        result["code"] = stdout.channel.recv_exit_status()
        result["ok"] = result["code"] == 0
    except Exception as e:
        result["err"] = f"{type(e).__name__}: {e}"
    finally:
        client.close()
    result["elapsed"] = time.time() - start
    return result


def render_result(result: dict) -> None:
    if result.get("code") is None:
        st.error(f"Errore di connessione/esecuzione: {result['err']}")
    else:
        icon = "✅" if result["ok"] else "❌"
        st.markdown(
            f"{icon} Exit code: **{result['code']}** — durata: {result['elapsed']:.2f}s"
        )
        with st.expander("Output (stdout)", expanded=True):
            st.code(result["out"] or "(nessuna output)", language="bash")
        if result["err"]:
            with st.expander("Stderr", expanded=not result["ok"]):
                st.code(result["err"], language="bash")


def host_panel(index: int, host_cfg: dict) -> None:
    host_id = host_cfg.get("id") or f"host-{index + 1}"
    uid = f"{host_id}#{index}"
    cmd_key = f"cmd_{uid}"
    pwd_key = f"pwd_{uid}"
    res_key = f"result_{uid}"

    with st.container(border=True):
        st.subheader(host_cfg.get("label", host_cfg["host"]))
        st.caption(
            f"`{host_cfg.get('user')}@{host_cfg['host']}`:port {host_cfg.get('port', DEFAULT_PORT)}"
        )
        if host_cfg.get("key_file"):
            st.caption(
                f"Autenticazione: chiave `{os.path.expanduser(host_cfg['key_file'])}`"
            )
        else:
            st.caption("Autenticazione: password (inserita di seguito)")

        st.text_input("Comando da eseguire", key=cmd_key, value=host_cfg.get("command", ""))
        use_password = not host_cfg.get("key_file")
        password = None
        if use_password:
            password = st.text_input(
                "Password", type="password", key=pwd_key, placeholder="(solo in memoria di sessione)"
            ) or None

        if st.button("Esegui via SSH", type="primary", key=f"run_{uid}", use_container_width=True):
            with st.spinner(f"Connessione a {host_cfg['host']}..."):
                st.session_state[res_key] = run_ssh(host_cfg, st.session_state[cmd_key], password)
            st.session_state[f"run_{uid}"] = False

        result = st.session_state.get(res_key)
        if result:
            render_result(result)


def main() -> None:
    cfg = load_config()
    hosts = cfg["hosts"]

    st.title("SSH Remote Console")
    st.caption(
        f"Configurazione: `{CONFIG_PATH}` — {len(hosts)} host configurati. "
        "I comandi correnti sono quelli del file JSON, modificabili qui sopra."
    )

    if st.button("Esegui su tutti gli host", type="secondary", use_container_width=False):
        jobs = []
        for i, h in enumerate(hosts):
            uid = f"{h.get('id') or f'host-{i + 1}'}#{i}"
            cmd = st.session_state.get(f"cmd_{uid}", h.get("command", ""))
            password = st.session_state.get(f"pwd_{uid}") or None
            jobs.append((h, cmd, password))
        with st.spinner("Esecuzione in parallelo..."):
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                results = list(
                    pool.map(lambda j: run_ssh(j[0], j[1], j[2]), jobs)
                )
        for i, (h, _cmd, _pwd) in enumerate(jobs):
            uid = f"{h.get('id') or f'host-{i + 1}'}#{i}"
            st.session_state[f"result_{uid}"] = results[i]

    if len(hosts) == 2:
        col1, col2 = st.columns(2)
        with col1:
            host_panel(0, hosts[0])
        with col2:
            host_panel(1, hosts[1])
    else:
        for i, h in enumerate(hosts):
            host_panel(i, h)

    res_keys = [k for k in st.session_state if k.startswith("result_")]
    if res_keys and st.button("Pulisci risultati"):
        for k in res_keys:
            del st.session_state[k]
        st.rerun()


main()
