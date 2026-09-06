"""Explicit deployment settings. Never log credentials."""
import os
import logging
from dataclasses import dataclass, field
from pathlib import Path

DISABLED_FEATURES = ('shell_tool','shell_snapshot','unified_exec','apply_patch_freeform',
    'view_image','web_search','web_search_request','browser_use','computer_use',
    'apps','connectors','plugins','remote_plugin','hooks','plugin_hooks','codex_hooks',
    'multi_agent','multi_agent_v2','collab','image_generation','js_repl','code_mode',
    'code_mode_only','memory_tool','memories','request_permissions_tool','tool_search',
    'tool_suggest','skill_search','workspace_dependencies','in_app_local_automation')

@dataclass
class Settings:
    binary: str = field(default_factory=lambda: os.getenv('CR_CODEX_BIN','/opt/codex-relay/bin/codex-app-server'))
    codex_home: str = field(default_factory=lambda: os.getenv('CR_CODEX_HOME','/var/lib/codex-relay/codex'))
    work_dir: str = field(default_factory=lambda: os.getenv('CR_WORK_DIR','/var/lib/codex-relay/work'))
    db: str = field(default_factory=lambda: os.getenv('CR_DB','/var/lib/codex-relay/relay.sqlite3'))
    host: str = field(default_factory=lambda: os.getenv('CR_LISTEN_HOST','127.0.0.1'))
    port: int = field(default_factory=lambda: int(os.getenv('CR_LISTEN_PORT','18021')))
    model: str = field(default_factory=lambda: os.getenv('CR_DEFAULT_MODEL','gpt-6-astra'))
    concurrent: int = 6
    queue_limit: int = 16
    queue_timeout: int = 120
    turn_timeout: int = 600
    tool_wait_timeout: int = 900
    max_live_sessions_per_user: int = 16
    history_ttl: int = 7 * 86400
    history_max_bytes_per_user: int = 100 * 1024 * 1024
    max_body: int = 20 * 1024 * 1024
    output_reserve: int = 8192
    extra_args: tuple = ()

def setup_logging():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    return logging.getLogger('codex-relay')

def validate(settings):
    if settings.host not in ('127.0.0.1','::1'):
        raise RuntimeError('Relay must listen on loopback; use an authenticated encrypted transport.')
    if not Path(settings.binary).is_file():raise RuntimeError('Official app-server binary missing')
