#!/bin/bash
# setup.sh — One-time setup for Claude Autopilot on Mac Mini
set -e

echo "🤖 Claude Autopilot Setup"
echo "========================="

# Check prerequisites
echo ""
echo "Checking prerequisites..."

check_cmd() {
    if command -v "$1" &> /dev/null; then
        echo "  ✅ $1 found: $(command -v "$1")"
    else
        echo "  ❌ $1 not found. Install it first."
        MISSING=1
    fi
}

MISSING=0
check_cmd node
check_cmd npm
check_cmd python3
check_cmd git
check_cmd tmux
check_cmd claude

if [ "$MISSING" -eq 1 ]; then
    echo ""
    echo "Missing prerequisites. Install them:"
    echo "  brew install node python tmux git"
    echo "  npm install -g @anthropic-ai/claude-code"
    exit 1
fi

echo ""
echo "Node version: $(node --version)"
echo "Python version: $(python3 --version)"
echo "Claude Code: $(claude --version 2>/dev/null || echo 'unknown')"

# Install Python dependencies
echo ""
echo "Installing Python dependencies..."
pip3 install -r requirements.txt --break-system-packages 2>/dev/null || pip3 install -r requirements.txt

# Set up .env if not exists
if [ ! -f .env ]; then
    echo ""
    echo "Creating .env from template..."
    cp .env.example .env
    echo "⚠️  Edit .env with your API keys before starting!"
    echo "   nano .env"
fi

# Create directories
mkdir -p tasks logs

# Create the tmux launch script
cat > launch.sh << 'LAUNCH'
#!/bin/bash
# launch.sh — Start Claude Autopilot in a persistent tmux session
SESSION="autopilot"
DIR="$(cd "$(dirname "$0")" && pwd)"

# Kill existing session if any
tmux kill-session -t $SESSION 2>/dev/null || true

# Create new session
tmux new-session -d -s $SESSION -c "$DIR"
tmux send-keys -t $SESSION "cd $DIR && python3 bot.py" Enter

echo "🤖 Claude Autopilot started in tmux session '$SESSION'"
echo "   Attach: tmux attach -t $SESSION"
echo "   Detach: Ctrl+B then D"
echo "   Kill:   tmux kill-session -t $SESSION"
LAUNCH
chmod +x launch.sh

# Create the launchd plist for auto-start on Mac
PLIST_PATH="$HOME/Library/LaunchAgents/com.autopilot.claude.plist"
cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.autopilot.claude</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(which python3)</string>
        <string>$(pwd)/bot.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$(pwd)</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$(pwd)/logs/bot-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$(pwd)/logs/bot-stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:$HOME/.npm-global/bin</string>
    </dict>
</dict>
</plist>
PLIST

echo ""
echo "✅ Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit .env with your API keys:"
echo "     nano .env"
echo ""
echo "  2. Get your Telegram chat ID:"
echo "     - Message your bot on Telegram"
echo "     - Run: curl https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
echo "     - Find your chat.id in the response"
echo "     - Add it to .env as TELEGRAM_CHAT_ID"
echo ""
echo "  3. Start the bot:"
echo "     ./launch.sh          # tmux session (recommended)"
echo "     # OR"
echo "     python3 bot.py       # foreground"
echo ""
echo "  4. (Optional) Auto-start on boot:"
echo "     launchctl load $PLIST_PATH"
echo ""
echo "  5. Open Telegram and send /help to your bot!"
