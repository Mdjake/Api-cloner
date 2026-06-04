import os
import json
import logging
import requests
from io import BytesIO
from typing import Dict, Any
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from groq import Groq

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


class APICloner:
    def __init__(self, groq_client):
        self.groq = groq_client

    async def analyze_api(self, api_url: str, method: str = 'GET') -> Dict[str, Any]:
        """Analyze API structure and response"""
        try:
            response = requests.request(method=method, url=api_url, timeout=10)
            response.raise_for_status()
            data = response.json()
            structure = self._extract_structure(data)
            return {
                'url': api_url,
                'method': method,
                'structure': structure,
                'sample_data': data,
                'status_code': response.status_code
            }
        except requests.exceptions.MissingSchema:
            raise Exception("Invalid URL format. Make sure to include https://")
        except requests.exceptions.ConnectionError:
            raise Exception("Could not connect to the URL. Check if it's reachable.")
        except requests.exceptions.Timeout:
            raise Exception("Request timed out after 10 seconds.")
        except ValueError:
            raise Exception("API did not return valid JSON.")
        except Exception as e:
            raise Exception(f"Failed to analyze API: {str(e)}")

    def _extract_structure(self, data: Any, depth: int = 0) -> Dict[str, Any]:
        """Extract structure of JSON data"""
        if depth > 5:
            return {"type": "max_depth_reached"}
        if isinstance(data, dict):
            return {"type": "object", "fields": {k: self._extract_structure(v, depth + 1) for k, v in data.items()}}
        elif isinstance(data, list):
            return {"type": "array", "items": self._extract_structure(data[0], depth + 1) if data else {"type": "unknown"}}
        elif isinstance(data, bool):
            return {"type": "boolean", "example": data}
        elif isinstance(data, str):
            return {"type": "string", "example": data[:50]}
        elif isinstance(data, (int, float)):
            return {"type": "number", "example": data}
        return {"type": "unknown"}

    def _generate_manipulation_code(self, manipulations: Dict[str, Any]) -> str:
        """Generate manipulation code based on user selections"""
        if not manipulations:
            return "    pass  # No manipulations applied"

        lines = []
        for key, action in manipulations.items():
            if action['action'] == 'delete':
                lines.append(f'    if isinstance(data, dict) and "{key}" in data:')
                lines.append(f'        del data["{key}"]')
            elif action['action'] == 'rename':
                lines.append(f'    if isinstance(data, dict) and "{key}" in data:')
                lines.append(f'        data["{action["new_name"]}"] = data.pop("{key}")')
            elif action['action'] == 'modify':
                lines.append(f'    if isinstance(data, dict) and "{key}" in data:')
                lines.append(f'        data["{key}"] = {repr(action["new_value"])}')
            elif action['action'] == 'extract':
                lines.append(f'    if isinstance(data, dict) and "{key}" in data:')
                lines.append(f'        extracted = data.pop("{key}")')
                lines.append(f'        data["{action["extract_key"]}"] = extracted')
        return '\n'.join(lines)

    def generate_clone_code(self, api_info: Dict[str, Any], manipulations: Dict[str, Any]) -> str:
        """Generate Python Flask code for API cloning"""
        manipulation_code = self._generate_manipulation_code(manipulations)

        # Build the code using a list to avoid .format() conflicts with curly braces
        code_lines = [
            "import os",
            "import json",
            "import requests",
            "from flask import Flask, jsonify, request",
            "from flask_cors import CORS",
            "",
            "app = Flask(__name__)",
            "CORS(app)",
            "",
            f'ORIGINAL_API_URL = "{api_info["url"]}"',
            f'ORIGINAL_METHOD = "{api_info["method"]}"',
            "",
            "",
            "def manipulate_response(data):",
            '    """Apply manipulations to the API response"""',
            "    if isinstance(data, dict):",
            "        data = data.copy()",
            manipulation_code,
            "    return data",
            "",
            "",
            "def fetch_original_api():",
            '    """Fetch data from original API"""',
            "    try:",
            "        response = requests.request(ORIGINAL_METHOD, ORIGINAL_API_URL, timeout=10)",
            "        response.raise_for_status()",
            "        return response.json()",
            "    except Exception as e:",
            '        return {"error": str(e)}',
            "",
            "",
            "@app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])",
            "@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])",
            "def catch_all(path):",
            '    """Main endpoint that returns manipulated API data"""',
            "    original_data = fetch_original_api()",
            "    manipulated_data = manipulate_response(original_data)",
            "    return jsonify(manipulated_data)",
            "",
            "",
            "@app.route('/health', methods=['GET'])",
            "def health_check():",
            '    """Health check endpoint"""',
            '    return jsonify({"status": "healthy", "cloned": True})',
            "",
            "",
            "if __name__ == '__main__':",
            "    port = int(os.environ.get('PORT', 5000))",
            "    app.run(host='0.0.0.0', port=port, debug=False)",
        ]
        return '\n'.join(code_lines)

    def generate_requirements(self) -> str:
        return (
            "flask==2.3.3\n"
            "flask-cors==4.0.0\n"
            "requests==2.31.0\n"
            "gunicorn==21.2.0\n"
            "python-dotenv==1.0.0\n"
        )

    async def explain_structure_with_ai(self, structure: Dict[str, Any]) -> str:
        """Use Groq AI to explain API structure and suggest manipulations"""
        prompt = (
            "Analyze this API response structure and suggest possible manipulations:\n"
            f"{json.dumps(structure, indent=2)}\n\n"
            "Provide:\n"
            "1. Brief explanation of the data structure\n"
            "2. Recommended fields to keep/delete\n"
            "3. Suggestions for renaming fields\n"
            "4. Suggestions for modifying field values\n"
            "Keep your response concise and practical."
        )
        try:
            completion = self.groq.chat.completions.create(
                model="llama3-70b-8192",  # Fixed: mixtral-8x7b-32768 is deprecated
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=1000
            )
            return completion.choices[0].message.content
        except Exception as e:
            return f"AI analysis unavailable: {str(e)}"


class TelegramBot:
    def __init__(self, token: str, groq_api_key: str):
        self.token = token
        self.groq_client = Groq(api_key=groq_api_key)
        self.api_cloner = APICloner(self.groq_client)
        self.user_data: Dict[int, Any] = {}

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        welcome_msg = (
            "🤖 *API Cloner Bot*\n\n"
            "Clone any REST API and customize its response!\n\n"
            "*Commands:*\n"
            "/clone — Start cloning an API\n"
            "/status — Show your current session\n"
            "/cancel — Cancel current operation\n"
            "/help — Show this message\n\n"
            "*Supported manipulations:*\n"
            "`delete:field` — Remove a field\n"
            "`rename:old:new` — Rename a field\n"
            "`modify:field:value` — Change a value\n"
            "`extract:field:new_key` — Extract a field"
        )
        await update.message.reply_text(welcome_msg, parse_mode='Markdown')

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self.start(update, context)

    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /cancel command"""
        user_id = update.effective_user.id
        if user_id in self.user_data:
            del self.user_data[user_id]
            await update.message.reply_text("✅ Session cancelled. Use /clone to start over.")
        else:
            await update.message.reply_text("No active session. Use /clone to start.")

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        user_id = update.effective_user.id
        if user_id not in self.user_data:
            await update.message.reply_text("No active session. Use /clone to start.")
            return

        data = self.user_data[user_id]
        step = data.get('step', 'unknown')
        manipulations = data.get('manipulations', {})
        api_info = data.get('api_info', {})

        msg = f"*Current Session:*\n"
        msg += f"Step: `{step}`\n"
        if api_info:
            msg += f"URL: `{api_info.get('url', 'N/A')}`\n"
            msg += f"Method: `{api_info.get('method', 'N/A')}`\n"
        msg += f"Manipulations registered: `{len(manipulations)}`"
        await update.message.reply_text(msg, parse_mode='Markdown')

    async def clone_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /clone command"""
        user_id = update.effective_user.id
        self.user_data[user_id] = {'step': 'awaiting_url', 'manipulations': {}}
        await update.message.reply_text(
            "📡 Send the API endpoint URL to clone.\n\n"
            "Examples:\n"
            "`https://api.example.com/data`\n"
            "`POST https://api.example.com/submit`\n\n"
            "Use /cancel to abort.",
            parse_mode='Markdown'
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Route messages based on current step"""
        user_id = update.effective_user.id
        text = update.message.text.strip()

        if user_id not in self.user_data:
            await update.message.reply_text("Use /clone to start cloning an API.")
            return

        step = self.user_data[user_id].get('step')

        if step == 'awaiting_url':
            await self.process_url(update, context, text)
        elif step == 'awaiting_manipulations':
            await self.process_manipulations(update, context, text)
        else:
            await update.message.reply_text("Use /clone to start over.")

    async def process_url(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        """Process API URL and analyze it"""
        user_id = update.effective_user.id

        # Parse method and URL
        parts = text.split()
        if len(parts) == 2 and parts[0].upper() in ('GET', 'POST', 'PUT', 'DELETE', 'PATCH'):
            method, url = parts[0].upper(), parts[1]
        else:
            method, url = 'GET', text

        # Basic URL validation
        if not url.startswith(('http://', 'https://')):
            await update.message.reply_text(
                "⚠️ Invalid URL. Please include the protocol.\n"
                "Example: `https://api.example.com/data`",
                parse_mode='Markdown'
            )
            return

        status_msg = await update.message.reply_text("🔄 Analyzing API...")

        try:
            api_info = await self.api_cloner.analyze_api(url, method)
            self.user_data[user_id]['api_info'] = api_info
            self.user_data[user_id]['step'] = 'awaiting_manipulations'

            structure_preview = json.dumps(api_info['structure'], indent=2)
            if len(structure_preview) > 800:
                structure_preview = structure_preview[:800] + "\n... (truncated)"

            await status_msg.edit_text(
                f"✅ *API Analyzed!*\n\n"
                f"📡 URL: `{url}`\n"
                f"🔧 Method: `{method}`\n"
                f"📊 Status: `{api_info['status_code']}`\n\n"
                f"*Structure:*\n```json\n{structure_preview}\n```",
                parse_mode='Markdown'
            )

            ai_msg = await update.message.reply_text("🤖 Getting AI suggestions...")
            ai_suggestions = await self.api_cloner.explain_structure_with_ai(api_info['structure'])

            await ai_msg.edit_text(
                f"🤖 *AI Suggestions:*\n\n{ai_suggestions}\n\n"
                "─────────────────────\n"
                "*Now send your manipulations* (one per line):\n"
                "`delete:field_name`\n"
                "`rename:old_name:new_name`\n"
                "`modify:field_name:new_value`\n"
                "`extract:field_name:new_key`\n\n"
                "Send `done` to generate code or `skip` to generate without changes.",
                parse_mode='Markdown'
            )

        except Exception as e:
            await status_msg.edit_text(
                f"❌ *Error:* {str(e)}\n\nTry a different URL or use /cancel.",
                parse_mode='Markdown'
            )

    async def process_manipulations(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
        """Process manipulation commands"""
        user_id = update.effective_user.id
        text_lower = text.lower().strip()

        if text_lower in ('done', 'skip'):
            if text_lower == 'skip':
                self.user_data[user_id]['manipulations'] = {}
            await self.generate_and_send_code(update, user_id)
            return

        manipulations = self.user_data[user_id].get('manipulations', {})
        feedback = []

        for line in text.strip().split('\n'):
            line = line.strip()
            if not line:
                continue

            parts = line.split(':')
            action = parts[0].lower() if parts else ''

            if action == 'delete' and len(parts) >= 2:
                field = parts[1]
                manipulations[field] = {'action': 'delete'}
                feedback.append(f"✓ Delete `{field}`")

            elif action == 'rename' and len(parts) == 3:
                manipulations[parts[1]] = {'action': 'rename', 'new_name': parts[2]}
                feedback.append(f"✓ Rename `{parts[1]}` → `{parts[2]}`")

            elif action == 'modify' and len(parts) >= 3:
                value = ':'.join(parts[2:])  # support values containing colons
                manipulations[parts[1]] = {'action': 'modify', 'new_value': value}
                feedback.append(f"✓ Modify `{parts[1]}` → `{value}`")

            elif action == 'extract' and len(parts) == 3:
                manipulations[parts[1]] = {'action': 'extract', 'extract_key': parts[2]}
                feedback.append(f"✓ Extract `{parts[1]}` as `{parts[2]}`")

            else:
                feedback.append(f"⚠️ Invalid: `{line}`")

        self.user_data[user_id]['manipulations'] = manipulations

        summary = '\n'.join(feedback)
        await update.message.reply_text(
            f"{summary}\n\n"
            f"📝 *Total manipulations:* {len(manipulations)}\n"
            "Send more, `done` to finish, or `skip` to reset all.",
            parse_mode='Markdown'
        )

    async def generate_and_send_code(self, update: Update, user_id: int):
        """Generate clone code and send as files"""
        data = self.user_data.get(user_id)
        if not data or 'api_info' not in data:
            await update.message.reply_text("❌ Session data missing. Use /clone to start over.")
            return

        await update.message.reply_text("⚙️ Generating your API clone...")

        api_info = data['api_info']
        manipulations = data.get('manipulations', {})

        try:
            clone_code = self.api_cloner.generate_clone_code(api_info, manipulations)
            requirements = self.api_cloner.generate_requirements()

            # Send main.py — Fixed: use BytesIO instead of tuple
            await update.message.reply_document(
                document=BytesIO(clone_code.encode('utf-8')),
                filename='main.py',
                caption="📄 *main.py* — Your cloned API server",
                parse_mode='Markdown'
            )

            # Send requirements.txt
            await update.message.reply_document(
                document=BytesIO(requirements.encode('utf-8')),
                filename='requirements.txt',
                caption="📦 *requirements.txt* — Install dependencies",
                parse_mode='Markdown'
            )

            await update.message.reply_text(
                "🚀 *Deployment Instructions:*\n\n"
                "*Local:*\n"
                "```bash\n"
                "pip install -r requirements.txt\n"
                "python main.py\n"
                "```\n\n"
                "*Railway / Render / Heroku:*\n"
                "1. Push files to a GitHub repo\n"
                "2. Connect repo to your platform\n"
                "3. Set `PORT` env variable if needed\n\n"
                "Your API will be live at `/` and health check at `/health` ✅\n\n"
                "Use /clone to clone another API.",
                parse_mode='Markdown'
            )

        except Exception as e:
            await update.message.reply_text(f"❌ Code generation failed: {str(e)}")
        finally:
            # Always clean up session
            if user_id in self.user_data:
                del self.user_data[user_id]

    def run(self):
        """Start the bot"""
        app = Application.builder().token(self.token).build()

        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("help", self.help_command))       # Fixed: was missing
        app.add_handler(CommandHandler("cancel", self.cancel_command))   # Fixed: was missing
        app.add_handler(CommandHandler("clone", self.clone_command))
        app.add_handler(CommandHandler("status", self.status_command))   # New
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        logger.info("Bot is running...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
    GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')

    if not TELEGRAM_TOKEN or not GROQ_API_KEY:
        raise ValueError("Set TELEGRAM_TOKEN and GROQ_API_KEY environment variables.")

    bot = TelegramBot(token=TELEGRAM_TOKEN, groq_api_key=GROQ_API_KEY)
    bot.run()