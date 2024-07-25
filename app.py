import os
from datetime import datetime
import asyncio
import html
import logging
from dataclasses import dataclass
from http import HTTPStatus

import uvicorn
from asgiref.wsgi import WsgiToAsgi
from flask import Flask, Response, abort, make_response, request

from telegram import Update, ReplyKeyboardMarkup, ForceReply
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackContext,
    CommandHandler,
    ContextTypes,
    ExtBot,
    TypeHandler,
    MessageHandler,
    ConversationHandler,
    filters
)

from dotenv import load_dotenv

import firefly

# Load environment variables from .env file
load_dotenv()

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Get environment variables
TOKEN = os.getenv('TG_BOT_TOKEN')
CATEGORIES = [category.strip() for category in os.getenv('CATEGORIES', '').split(',')]
SOURCES = [source.strip() for source in os.getenv('SOURCES', '').split(',')]
URL = os.getenv('WEBHOOK_URL')
PORT = os.getenv('PORT')
ADMIN_CHAT_ID = 123456

# Conversation states
AMOUNT, DESCRIPTION, CATEGORY, SOURCE = range(4)

# Store user's transaction data
user_data = {}

@dataclass
class WebhookUpdate:
    """Simple dataclass to wrap a custom update type"""

    user_id: int
    payload: str


class CustomContext(CallbackContext[ExtBot, dict, dict, dict]):
    """
    Custom CallbackContext class that makes `user_data` available for updates of type
    `WebhookUpdate`.
    """

    @classmethod
    def from_update(
        cls,
        update: object,
        application: "Application",
    ) -> "CustomContext":
        if isinstance(update, WebhookUpdate):
            return cls(application=application, user_id=update.user_id)
        return super().from_update(update, application)

""" Defining bot functions here """
def enter_transaction(trans_datetime, amount, description, category_name, source_name):
    # This function is a placeholder. Implement your actual logic here.
    try:
        response = firefly.enter_transaction(trans_datetime, amount, description, category_name, source_name)
        logger.debug(response.json())
        logger.info(f"Transaction entered: {trans_datetime}, {amount}, {description}, {category_name}, {source_name}")
        return True
    except Exception as e:
        logger.debug(e)
        return False

async def start(update: Update, context: CustomContext) -> None:
    await update.message.reply_text("Welcome! I'm a bot that helps you enter transactions. Use /enter_transaction to start.")

async def start_transaction(update: Update, context):
    user_data[update.effective_user.id] = {'trans_datetime': datetime.now().strftime("%Y-%m-%dT%H:%M:%S%z")}
    await update.message.reply_text("Enter the transaction amount:", reply_markup=ForceReply())
    return AMOUNT

async def process_amount(update: Update, context):
    try:
        amount = float(update.message.text)
        user_data[update.effective_user.id]['amount'] = amount
        await update.message.reply_text("Enter a description for the transaction:", reply_markup=ForceReply())
        return DESCRIPTION
    except ValueError:
        await update.message.reply_text("Invalid amount. Please enter a number.")
        return AMOUNT

async def process_description(update: Update, context):
    user_data[update.effective_user.id]['description'] = update.message.text
    markup = ReplyKeyboardMarkup([[category] for category in CATEGORIES], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Select a category:", reply_markup=markup)
    return CATEGORY

async def process_category(update: Update, context):
    if update.message.text not in CATEGORIES:
        await update.message.reply_text("Please select a valid category.")
        return CATEGORY
    user_data[update.effective_user.id]['category_name'] = update.message.text
    markup = ReplyKeyboardMarkup([[source] for source in SOURCES], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("Select a source:", reply_markup=markup)
    return SOURCE

async def process_source(update: Update, context):
    if update.message.text not in SOURCES:
        await update.message.reply_text("Please select a valid source.")
        return SOURCE
    user_data[update.effective_user.id]['source_name'] = update.message.text
    await submit_transaction(update, context)
    return ConversationHandler.END

async def submit_transaction(update: Update, context):
    user_id = update.effective_user.id
    data = user_data[user_id]
    success = enter_transaction(
        data['trans_datetime'],
        data['amount'],
        data['description'],
        data['category_name'],
        data['source_name']
    )
    if success:
        await update.message.reply_text("Transaction entered successfully!")
    else:
        await update.message.reply_text("Failed to enter transaction. Please try again.")
    del user_data[user_id]

async def webhook_update(update: WebhookUpdate, context: CustomContext) -> None:
    """Handle custom updates."""
    chat_member = await context.bot.get_chat_member(chat_id=update.user_id, user_id=update.user_id)
    payloads = context.user_data.setdefault("payloads", [])
    payloads.append(update.payload)
    combined_payloads = "</code>\n• <code>".join(payloads)
    text = (
        f"The user {chat_member.user.mention_html()} has sent a new payload. "
        f"So far they have sent the following payloads: \n\n• <code>{combined_payloads}</code>"
    )
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode=ParseMode.HTML)


async def main() -> None:
    """Set up PTB application and a web application for handling the incoming requests."""
    context_types = ContextTypes(context=CustomContext)
    # Here we set updater to None because we want our custom webhook server to handle the updates
    # and hence we don't need an Updater instance
    application = (
        Application.builder().token(TOKEN).updater(None).context_types(context_types).build()
    )

    # register handlers
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('enter_transaction', start_transaction)],
        states={
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_amount)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_description)],
            CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_category)],
            SOURCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_source)],
        },
        fallbacks=[],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(conv_handler)
    application.add_handler(TypeHandler(type=WebhookUpdate, callback=webhook_update))

    # Pass webhook settings to telegram
    await application.bot.set_webhook(url=f"{URL}/telegram", allowed_updates=Update.ALL_TYPES)

    # Set up webserver
    flask_app = Flask(__name__)

    @flask_app.post("/telegram")  # type: ignore[misc]
    async def telegram() -> Response:
        """Handle incoming Telegram updates by putting them into the `update_queue`"""
        await application.update_queue.put(Update.de_json(data=request.json, bot=application.bot))
        return Response(status=HTTPStatus.OK)

    @flask_app.route("/submitpayload", methods=["GET", "POST"])  # type: ignore[misc]
    async def custom_updates() -> Response:
        """
        Handle incoming webhook updates by also putting them into the `update_queue` if
        the required parameters were passed correctly.
        """
        try:
            user_id = int(request.args["user_id"])
            payload = request.args["payload"]
        except KeyError:
            abort(
                HTTPStatus.BAD_REQUEST,
                "Please pass both `user_id` and `payload` as query parameters.",
            )
        except ValueError:
            abort(HTTPStatus.BAD_REQUEST, "The `user_id` must be a string!")

        await application.update_queue.put(WebhookUpdate(user_id=user_id, payload=payload))
        return Response(status=HTTPStatus.OK)

    @flask_app.get("/healthcheck")  # type: ignore[misc]
    async def health() -> Response:
        """For the health endpoint, reply with a simple plain text message."""
        response = make_response("The bot is still running fine :)", HTTPStatus.OK)
        response.mimetype = "text/plain"
        return response

    webserver = uvicorn.Server(
        config=uvicorn.Config(
            app=WsgiToAsgi(flask_app),
            port=PORT,
            use_colors=False,
            host="0.0.0.0",
        )
    )

    # Run application and webserver together
    async with application:
        await application.start()
        await webserver.serve()
        await application.stop()


if __name__ == "__main__":
    asyncio.run(main())