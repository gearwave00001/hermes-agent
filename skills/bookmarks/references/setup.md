# Mnemosyne Setup — Installation & Configuration

## Prerequisites
- Hermes Agent running with `~/.hermes/hermes-agent/venv`
- GPU server with embedding model (Qwen3-Embedding-8B-FP8-DYNAMIC on vLLM at 192.168.1.225:5679)

## Installation steps

1. **Install packages in BOTH venvs** (workspace `.venv` AND `~/.hermes/hermes-agent/venv`):
   ```bash
   ~/.hermes/hermes-agent/venv/bin/python -m ensurepip
   ~/.hermes/hermes-agent/venv/bin/python -m pip install mnemosyne-hermes "mnemosyne-memory[all]"
   ```

2. **Install the Hermes plugin** (separate from pip — creates symlink at `~/.hermes/plugins/mnemosyne`):
   ```bash
   ~/.hermes/hermes-agent/venv/bin/python -m mnemosyne_hermes.install
   ```
   Without this, `hermes memory status` shows "Plugin: NOT installed" and no embedding traffic occurs during conversation.

3. **Configure Hermes**:
   ```bash
   hermes config set memory.provider mnemosyne
   hermes config set memory.memory_enabled false
   hermes config set memory.user_profile_enabled false
   ```

4. **Set env vars** (add to `~/.bashrc` for persistence):
   ```bash
   export MNEMOSYNE_EMBEDDING_API_URL=http://192.168.1.225:5679/v1
   export MNEMOSYNE_EMBEDDING_MODEL=Qwen3-Embedding-8B-FP8-DYNAMIC
   export MNEMOSYNE_EMBEDDING_DIM=4096
   export MNEMOSYNE_EMBEDDING_API_KEY=
   ```

5. **Symlink CLIs to PATH** (if `~/.local/bin` not on PATH):
   ```bash
   ln -sf ~/.hermes/hermes-agent/venv/bin/mnemosyne ~/.local/bin/mnemosyne
   ln -sf ~/.hermes/hermes-agent/venv/bin/mnemosyne-hermes ~/.local/bin/mnemosyne-hermes
   ```

## Verification
```bash
hermes memory status    # should show "Plugin: installed ✓" and "mnemosyne (local) ← active"
mnemosyne stats         # should show DB path and bank list
```

## Common pitfalls

- **Dimension mismatch**: Qwen3-Embedding-8B produces 4096-dim vectors. Mnemosyne defaults to 384 (bge-small). Set `MNEMOSYNE_EMBEDDING_DIM=4096` or vector inserts fail.
- **Plugin not installed**: `pip install mnemosyne-hermes` installs the Python package but does NOT register it with Hermes. Must run `python -m mnemosyne_hermes.install` separately.
- **Config resets on sandbox restart**: `memory.provider: mnemosyne` may be wiped. Re-run `hermes config set memory.provider mnemosyne` after restart.
- **System Python vs venv Python**: Scripts using Mnemosyne must use the venv Python (`~/.hermes/hermes-agent/venv/bin/python`), not `/usr/bin/env python3`.
- **Bank creation not idempotent**: `BankManager().create_bank()` raises `ValueError` if bank exists. Wrap in try/except.

## Memory banks
- `default` — session memories, preferences, corrections (auto-injected via pre_llm_call hook)
- `bookmarks` — URL bookmarks (only queried by bookmarks skill, never pollutes context)
- Banks are isolated — recall on one bank never returns results from another.

## Data location
- DB: `~/.hermes/mnemosyne/data/mnemosyne.db` (single SQLite file)
- Banks dir: `~/.hermes/mnemosyne/data/banks/`
- Can be moved via `MNEMOSYNE_DATA_DIR` env var
