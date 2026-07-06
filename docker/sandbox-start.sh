SANDBOX_NAME="shell-hermes-agent"
HOST_IP="0.0.0.0"
HERMES_MNEMOSYNE_MAIN_PORT="8765"
HERMES_MNEMOSYNE_BOOKMARKS_PORT="8766"
CAMOFOX_AGENT_PORT="9377"
CAMOFOX_VNC_PORT="6080"

echo "Starting Docker Sandbox: ${SANDBOX_NAME}..."
sbx run --name "${SANDBOX_NAME}" -d

echo "Publishing port mapping (${HOST_IP}:${HERMES_MNEMOSYNE_MAIN_PORT}:${HERMES_MNEMOSYNE_MAIN_PORT})..."
sbx ports "${SANDBOX_NAME}" --publish "${HOST_IP}:${HERMES_MNEMOSYNE_MAIN_PORT}:${HERMES_MNEMOSYNE_MAIN_PORT}"

echo "Publishing port mapping (${HOST_IP}:${HERMES_MNEMOSYNE_BOOKMARKS_PORT}:${HERMES_MNEMOSYNE_BOOKMARKS_PORT})..."
sbx ports "${SANDBOX_NAME}" --publish "${HOST_IP}:${HERMES_MNEMOSYNE_BOOKMARKS_PORT}:${HERMES_MNEMOSYNE_BOOKMARKS_PORT}"

echo "Publishing port mapping (${HOST_IP}:${CAMOFOX_VNC_PORT}:${CAMOFOX_VNC_PORT})..."
sbx ports "${SANDBOX_NAME}" --publish "${HOST_IP}:${CAMOFOX_VNC_PORT}:${CAMOFOX_VNC_PORT}"

echo "Publishing port mapping (${HOST_IP}:${CAMOFOX_AGENT_PORT}:${CAMOFOX_AGENT_PORT})..."
sbx ports "${SANDBOX_NAME}" --publish "${HOST_IP}:${CAMOFOX_AGENT_PORT}:${CAMOFOX_AGENT_PORT}"

echo "Attaching to sandbox shell..."
sbx run --name "${SANDBOX_NAME}"