#!/usr/bin/env bash
# setup_kapso.sh — Configure Kapso voice agent for GauchOS (self-hosted)
#
# Prerequisites:
#   - KAPSO_API_KEY: Your Kapso API key
#   - WHATSAPP_CONFIG_ID: Your WhatsApp configuration ID (from Kapso dashboard)
#   - VPS_BASE_URL: Your server's public URL (e.g., https://voice.yourdomain.com)
#
# Usage:
#   export KAPSO_API_KEY="your_key"
#   export WHATSAPP_CONFIG_ID="your_config_id"
#   export VPS_BASE_URL="https://voice.yourdomain.com"
#   bash setup_kapso.sh

set -euo pipefail

KAPSO_BASE="https://app.kapso.ai/api/v1"

for var in KAPSO_API_KEY WHATSAPP_CONFIG_ID VPS_BASE_URL; do
  if [ -z "${!var:-}" ]; then
    echo "ERROR: $var is not set"
    exit 1
  fi
done

echo "=== GauchOS Voice Agent — Kapso Setup ==="
echo ""

# Step 1: Enable voice calls
echo "Step 1: Enabling voice calls on WhatsApp config ${WHATSAPP_CONFIG_ID}..."

ENABLE_RESPONSE=$(curl -s -w "\n%{http_code}" -X PATCH \
  "${KAPSO_BASE}/whatsapp_configs/${WHATSAPP_CONFIG_ID}" \
  -H "X-API-Key: ${KAPSO_API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"whatsapp_config": {"calls_enabled": true}}')

HTTP_CODE=$(echo "$ENABLE_RESPONSE" | tail -1)
BODY=$(echo "$ENABLE_RESPONSE" | head -n -1)

if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
  echo "  ✓ Voice calls enabled"
else
  echo "  ✗ Failed (HTTP $HTTP_CODE): $BODY"
  exit 1
fi

echo ""

# Step 2: Create voice agent
echo "Step 2: Creating voice agent 'gauchOS-voice-agent'..."

AGENT_RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
  "${KAPSO_BASE}/voice_agents" \
  -H "X-API-Key: ${KAPSO_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{
    \"voice_agent\": {
      \"name\": \"gauchOS-voice-agent\",
      \"provider\": {
        \"kind\": \"pipecat\",
        \"agent_name\": \"gauchOS-voice-agent\",
        \"base_url\": \"${VPS_BASE_URL}\"
      }
    }
  }")

HTTP_CODE=$(echo "$AGENT_RESPONSE" | tail -1)
BODY=$(echo "$AGENT_RESPONSE" | head -n -1)

if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
  VOICE_AGENT_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('data',{}).get('id',''))" 2>/dev/null || echo "")
  if [ -z "$VOICE_AGENT_ID" ]; then
    VOICE_AGENT_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || echo "")
  fi
  echo "  ✓ Voice agent created: ${VOICE_AGENT_ID}"
else
  echo "  ✗ Failed (HTTP $HTTP_CODE): $BODY"
  exit 1
fi

if [ -z "$VOICE_AGENT_ID" ]; then
  echo "  ✗ Could not extract voice agent ID from response"
  echo "  Response: $BODY"
  exit 1
fi

echo ""

# Step 3: Assign WhatsApp number
echo "Step 3: Assigning WhatsApp number to voice agent..."

ASSIGN_RESPONSE=$(curl -s -w "\n%{http_code}" -X POST \
  "${KAPSO_BASE}/voice_agents/${VOICE_AGENT_ID}/voice_agent_whatsapp_configs" \
  -H "X-API-Key: ${KAPSO_API_KEY}" \
  -H "Content-Type: application/json" \
  -d "{
    \"voice_agent_whatsapp_config\": {
      \"whatsapp_config_id\": \"${WHATSAPP_CONFIG_ID}\",
      \"is_primary\": true,
      \"enabled\": true
    }
  }")

HTTP_CODE=$(echo "$ASSIGN_RESPONSE" | tail -1)
BODY=$(echo "$ASSIGN_RESPONSE" | head -n -1)

if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
  echo "  ✓ WhatsApp number assigned to voice agent"
else
  echo "  ✗ Failed (HTTP $HTTP_CODE): $BODY"
  exit 1
fi

echo ""
echo "=== Setup complete! ==="
echo ""
echo "Voice Agent ID: ${VOICE_AGENT_ID}"
echo "Server URL: ${VPS_BASE_URL}"
echo "WhatsApp Config: ${WHATSAPP_CONFIG_ID}"
echo ""
echo "Test: curl ${VPS_BASE_URL}/health"
echo "Then call your WhatsApp number."
