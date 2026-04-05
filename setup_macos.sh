#!/bin/bash
# ============================================================
# Expense Agent — macOS LaunchAgent Setup
# Project: ~/Documents/projects/ExpenseAgent
# ============================================================

PROJECT_DIR="$HOME/Documents/projects/ExpenseAgent"
PYTHON="$PROJECT_DIR/env/bin/python"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_FILE="$PLIST_DIR/com.expenseagent.bot.plist"
LOG_DIR="$PROJECT_DIR/logs"

if [ -z "$DISCORD_EXPENSE_BOT_TOKEN" ]; then
    echo "❌ DISCORD_EXPENSE_BOT_TOKEN is not set."
    echo "   Run: export DISCORD_EXPENSE_BOT_TOKEN=your_token_here"
    exit 1
fi

echo "==> Creating directories..."
mkdir -p "$LOG_DIR"
mkdir -p "$PROJECT_DIR/data"

echo "==> Setting up virtual environment..."
if [ ! -f "$PYTHON" ]; then
    python3 -m venv "$PROJECT_DIR/env"
    "$PROJECT_DIR/env/bin/pip" install -r "$PROJECT_DIR/requirements.txt"
fi

# Find Ollama path
OLLAMA_PATH=$(which ollama 2>/dev/null || echo "/usr/local/bin/ollama")
echo "==> Ollama found at: $OLLAMA_PATH"

echo "==> Writing LaunchAgent plist..."

cat > "$PLIST_FILE" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.expenseagent.bot</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PROJECT_DIR/env/bin/python</string>
        <string>$PROJECT_DIR/bot.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>DISCORD_EXPENSE_BOT_TOKEN</key>
        <string>$DISCORD_EXPENSE_BOT_TOKEN</string>
        <key>EXPENSE_CHANNEL_NAME</key>
        <string>expenses</string>
        <key>DIGEST_CHANNEL_NAME</key>
        <string>finance-digest</string>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>$LOG_DIR/bot.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/bot.error.log</string>
</dict>
</plist>
PLIST

echo "==> Loading LaunchAgent..."
launchctl unload "$PLIST_FILE" 2>/dev/null
launchctl load "$PLIST_FILE"

echo ""
echo "✅ Expense Agent is running."
echo ""
echo "Useful commands:"
echo "  Stop:    launchctl unload ~/Library/LaunchAgents/com.expenseagent.bot.plist"
echo "  Start:   launchctl load   ~/Library/LaunchAgents/com.expenseagent.bot.plist"
echo "  Status:  launchctl list | grep expenseagent"
echo "  Logs:    tail -f $LOG_DIR/bot.log"
echo "  Errors:  tail -f $LOG_DIR/bot.error.log"
