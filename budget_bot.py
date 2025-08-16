import telegram
import asyncio
import pytz
import re
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import json
import os
from datetime import date, timedelta, datetime
import google.generativeai as genai
from dotenv import load_dotenv
from actual import Actual
from actual.queries import get_transactions, get_categories as get_categories_from_actual_queries, get_accounts, reconcile_transaction
from actual.exceptions import UnknownFileId, ActualError
from sqlalchemy.orm.exc import MultipleResultsFound

load_dotenv() # Load environment variables from .env file

# --- Gemini API Configuration ---
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
genai.configure(api_key=GEMINI_API_KEY)

# --- Configuration ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
ACTUAL_API_URL = os.environ.get('ACTUAL_API_URL')
ACTUAL_BUDGET_ID = os.environ.get('ACTUAL_BUDGET_ID')
ACTUAL_CASH_ACCOUNT_ID = os.environ.get('ACTUAL_CASH_ACCOUNT_ID')
ACTUAL_PASSWORD = os.environ.get('ACTUAL_PASSWORD')

# --- Actual Budget API Functions ---

def add_transaction(date, amount, payee, notes, category_id):
    with Actual(base_url=ACTUAL_API_URL, password=ACTUAL_PASSWORD, file=ACTUAL_BUDGET_ID, cert=False) as actual:
        # actualpy amounts are in cents, so no conversion needed if amount is already in cents
        # For expenses, amount should be negative
        transaction_data = {
            "date": date,
            "amount": amount,
            "payee": payee,
            "notes": notes,
            "category_id": category_id,
            "account_id": ACTUAL_CASH_ACCOUNT_ID
        }
        actual.client.transactions.create(transaction_data)
        actual.commit() # Commit the changes to the server

def get_categories_from_actual():
    with Actual(base_url=ACTUAL_API_URL, password=ACTUAL_PASSWORD, file=ACTUAL_BUDGET_ID, cert=False) as actual:
        categories_data = get_categories_from_actual_queries(actual.session)
        return {category.name.lower(): category.id for category in categories_data}

def get_accounts_from_actual():
    with Actual(base_url=ACTUAL_API_URL, password=ACTUAL_PASSWORD, file=ACTUAL_BUDGET_ID, cert=False) as actual:
        accounts_data = get_accounts(actual.session)
        accounts_map = {}
        for account in accounts_data:
            accounts_map[str(account.id)] = account.name
        return accounts_map

def get_uncategorized_transactions(session):
    print("Fetching uncategorized transactions...")
    print("Actual instance initialized, fetching transactions...")
    # Fetch all transactions and then filter for uncategorized ones
    try:
        all_transactions = get_transactions(session) # Fetch all transactions
        uncategorized_transactions = [t for t in all_transactions if t.category is None]
    except Exception as e:
        print(f"Error fetching transactions: {e}")
        raise ConnectionError(f"Error fetching transactions: {e}")
    print(f"Found {len(uncategorized_transactions)} uncategorized transactions.")
    return uncategorized_transactions # Return Transaction objects directly

def get_transactions_in_range(session, start_date, end_date):
    transactions = get_transactions(session, date_start=start_date, date_end=end_date)
    return transactions # Return Transaction objects directly


# --- Telegram Bot Handlers ---
async def start(update, context):
    await update.message.reply_text('Welcome to your Budget Bot!')
    print(f"User {update.effective_user.id} started the bot.")

async def handle_message(update, context):
    print(f"Received message from user {update.effective_user.id}: {update.message.text}")
    text = update.message.text.lower()
    print(f"Received message: {text}")

    if text == "cancel" or text == "stop":
        await cancel_flow(update, context)
        return

    if text.startswith("add "):
        await add_expense(update, context)
    elif text.startswith("sort"):
        await sort_expense(update, context)
    elif text.startswith("spending"):
        await get_spending(update, context)
    elif text.startswith("ai"):
        await get_ai_suggestion(update, context)
    elif text.startswith("categories"):
        await get_categories(update)
    elif text.startswith("sync"):
        await sync_bank(update, context)
    else:
        await unrecognized_command(update, context)

async def get_categories(update):
    try:
        loop = asyncio.get_running_loop()
        categories = await loop.run_in_executor(None, get_categories_from_actual)
        category_names = "\n".join(sorted(categories))
        await update.message.reply_text(f'Available Categories:\n{category_names}')
    except (ConnectionError, ValueError) as e:
        await update.message.reply_text(f'API Error fetching categories: {e}')
    except Exception as e:
        await update.message.reply_text(f'Error: {e}')

async def add_expense(update, context):
    full_text = update.message.text
    # Remove the "add " prefix and then parse the rest
    if not full_text.lower().startswith("add "):
        await update.message.reply_text("Error: 'add' command must start with 'add '.")
        return

    text_after_prefix = full_text[len("add "):].strip()

    try:
        payee, amount_str = text_after_prefix.split(' ', 1)
        amount = int(float(amount_str) * 100) # Convert to cents

        try:
            loop = asyncio.get_running_loop()
            categories = await loop.run_in_executor(None, get_categories_from_actual)
            category_id = categories.get(payee.lower(), categories.get("misc"))

            from datetime import date
            today = date.today().strftime("%Y-%m-%d")

            add_transaction(today, -amount, payee, "", category_id)
            await update.message.reply_text(f'Added transaction: {payee} for ${amount_str}')
        except (ConnectionError, ValueError) as e:
            await update.message.reply_text(f'API Error: {e}')
        except Exception as e:
            await update.message.reply_text(f'Error: {e}')
    except (ValueError, IndexError) as e:
        await update.message.reply_text(f'Input Error: {e}. Please use format "add Payee Amount" (e.g., "add Groceries 5.99")')

async def sort_expense(update, context):
    try:
        # Determine the effective message object for replies
        message_to_reply = update.message if hasattr(update, 'message') else update

        print(f"User {message_to_reply.from_user.id} requested to sort expenses.")
        context.user_data['sorting_in_progress'] = True # Set sorting in progress flag

        with Actual(base_url=ACTUAL_API_URL, password=ACTUAL_PASSWORD, file=ACTUAL_BUDGET_ID, cert=False) as actual:
            uncategorized_transactions = get_uncategorized_transactions(actual.session)
            print(f"Found {len(uncategorized_transactions)} uncategorized transactions for user {message_to_reply.from_user.id}")

            if uncategorized_transactions:
                latest_transaction = uncategorized_transactions[0]
                context.user_data['sorting_transaction'] = latest_transaction.id
                
                accounts_map = get_accounts_from_actual()
                account_name = accounts_map.get(latest_transaction.account.id, 'Unknown Account')
                description = re.sub(r"\s+", ' ', latest_transaction.notes).strip()
                message_parts = [
                    'Uncategorized Expense:',
                    f'Description: {description or "N/A"}',
                    f'Account: {account_name}',
                    f'Payee: {latest_transaction.payee.name or "N/A"}',
                    f'Amount: ${abs(latest_transaction.amount / 100):.2f}',
                    f'Date: {datetime.strptime(str(latest_transaction.date), "%Y%m%d").strftime("%A, %m/%d/%Y")}',
                    f'Cleared: {latest_transaction.cleared}'
                ]
                full_message = "\n".join(message_parts) + '\n\nPlease select a category for this expense:'

                loop = asyncio.get_running_loop()
                categories_dict = await loop.run_in_executor(None, get_categories_from_actual)
                
                keyboard = []
                row = []
                for i, category_name in enumerate(sorted(categories_dict.keys())):
                    button = InlineKeyboardButton(category_name.title(), callback_data=f"sort_category_{category_name.lower()}")
                    row.append(button)
                    if (i + 1) % 3 == 0:
                        keyboard.append(row)
                        row = []
                if row:
                    keyboard.append(row)
                
                # Add a "Cancel" button
                keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_sort_flow")])

                reply_markup = InlineKeyboardMarkup(keyboard)

                await message_to_reply.reply_text(full_message, reply_markup=reply_markup)
                context.user_data['awaiting_category_for_sort'] = True
            else:
                await message_to_reply.reply_text('No uncategorized expenses found.')
                context.user_data['awaiting_category_for_sort'] = False
                context.user_data.pop('sorting_in_progress', None) # End sorting flow if no transactions

    except (ConnectionError, ValueError) as e:
        await message_to_reply.reply_text(f'API Error: {e}')
    except Exception as e:
        await message_to_reply.reply_text(f'Error: {e}')

async def handle_sort_reply(update, context):
    query = update.callback_query
    if query:
        await query.answer() # Acknowledge the callback query
        callback_data = query.data
        if callback_data.startswith("sort_category_"):
            category_name_lower = callback_data.replace("sort_category_", "")
            category_name_title_case = category_name_lower.title()
            transaction_id = context.user_data.get('sorting_transaction')

            try:
                loop = asyncio.get_running_loop()
                categories = await loop.run_in_executor(None, get_categories_from_actual)
                category_id = categories.get(category_name_lower)

                if category_id and transaction_id:
                    with Actual(base_url=ACTUAL_API_URL, password=ACTUAL_PASSWORD, file=ACTUAL_BUDGET_ID, cert=False) as actual:
                        print(f"Transaction ID: {transaction_id}")
                        print(f"Category ID: {category_id}")
                        
                        current_session_uncategorized = get_transactions(actual.session, category=None)
                        transaction_to_update = None
                        for t in current_session_uncategorized:
                            if str(t.id) == str(transaction_id):
                                transaction_to_update = t
                                break

                        if transaction_to_update:
                            reconcile_transaction(
                                s=actual.session,
                                date=transaction_to_update.get_date(),
                                account=transaction_to_update.account.id,
                                payee=transaction_to_update.payee.name,
                                notes=transaction_to_update.notes,
                                category=category_name_title_case,
                                amount=transaction_to_update.get_amount(),
                                imported_id=transaction_to_update.financial_id,
                                cleared=bool(transaction_to_update.cleared),
                                imported_payee=transaction_to_update.imported_description,
                                update_existing=True
                            )
                            actual.commit()
                            await query.edit_message_text(f'Expense categorized as {category_name_title_case}.')
                            context.user_data.pop('sorting_transaction', None)
                            if context.user_data.get('sorting_in_progress'):
                                await sort_expense(query.message, context) # Pass query.message to sort_expense
                            else:
                                context.user_data['awaiting_category_for_sort'] = False
                        else:
                            await query.edit_message_text(f'Transaction with ID {transaction_id} not found among uncategorized expenses in the current session.')
                            return
                else:
                    await query.edit_message_text(f'Category "{category_name_title_case}" not found or transaction ID missing. Please try again.')
            except (ConnectionError, ValueError) as e:
                await query.edit_message_text(f'API Error categorizing: {e}')
            except Exception as e:
                await query.edit_message_text(f'Error: {e}')
    else: # Handle text replies (e.g., "categories" command or direct category input)
        if context.user_data.get('awaiting_category_for_sort'):
            reply_text = update.message.text.strip()
            transaction_id = context.user_data.get('sorting_transaction')

            if reply_text.lower() == "categories":
                try:
                    loop = asyncio.get_running_loop()
                    categories = await loop.run_in_executor(None, get_categories_from_actual)
                    category_names = "\n".join(sorted(categories.keys()))
                    await update.message.reply_text(f'Available Categories:\n{category_names}\n\nPlease select a category from the keyboard or reply with a category name to categorize the expense.')
                except (ConnectionError, ValueError) as e:
                    await update.message.reply_text(f'API Error fetching categories: {e}')
                except Exception as e:
                    await update.message.reply_text(f'Error: {e}')
            else:
                category_name_title_case = reply_text.title()
                try:
                    loop = asyncio.get_running_loop()
                    categories = await loop.run_in_executor(None, get_categories_from_actual)
                    category_id = categories.get(category_name_title_case.lower())

                    if category_id and transaction_id:
                        with Actual(base_url=ACTUAL_API_URL, password=ACTUAL_PASSWORD, file=ACTUAL_BUDGET_ID, cert=False) as actual:
                            current_session_uncategorized = get_transactions(actual.session, category=None)
                            transaction_to_update = None
                            for t in current_session_uncategorized:
                                if str(t.id) == str(transaction_id):
                                    transaction_to_update = t
                                    break

                            if transaction_to_update:
                                reconcile_transaction(
                                    s=actual.session,
                                    date=transaction_to_update.get_date(),
                                    account=transaction_to_update.account.id,
                                    payee=transaction_to_update.payee.name,
                                    notes=transaction_to_update.notes,
                                    category=category_name_title_case,
                                    amount=transaction_to_update.get_amount(),
                                    imported_id=transaction_to_update.financial_id,
                                    cleared=bool(transaction_to_update.cleared),
                                    imported_payee=transaction_to_update.imported_description,
                                    update_existing=True
                                )
                                actual.commit()
                                await update.message.reply_text(f'Expense categorized as {category_name_title_case}.')
                                context.user_data.pop('sorting_transaction', None)
                                if context.user_data.get('sorting_in_progress'):
                                    await sort_expense(update, context) # This is a text message, so update.message is fine
                                else:
                                    context.user_data['awaiting_category_for_sort'] = False
                            else:
                                await update.message.reply_text(f'Transaction with ID {transaction_id} not found among uncategorized expenses in the current session.')
                                return
                    else:
                        await update.message.reply_text(f'Category "{category_name_title_case}" not found or transaction ID missing. Please try again or type "categories".')
                except (ConnectionError, ValueError) as e:
                    await update.message.reply_text(f'API Error categorizing: {e}')
                except Exception as e:
                    await update.message.reply_text(f'Error: {e}')

async def cancel_flow(update, context):
    context.user_data['awaiting_category_for_sort'] = False
    context.user_data.pop('sorting_transaction', None)
    context.user_data.pop('sorting_in_progress', None)
    
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Sorting flow cancelled.")
    else:
        await update.message.reply_text("Sorting flow cancelled.")

async def get_spending(update, context):
    full_text = update.message.text.lower()
    parts = full_text.split(' ')
    
    if len(parts) < 2:
        await update.message.reply_text("Please specify a period (day/week/month/year) and optionally 'simple' or 'detailed'.\n"
                                        "Example: spending month simple")
        return

    period = parts[1]
    detail_level = parts[2] if len(parts) > 2 else "simple" # Default to simple

    today = date.today()
    start_date = None
    end_date = today.strftime("%Y-%m-%d")

    if period == "day":
        start_date = today.strftime("%Y-%m-%d")
    elif period == "week":
        start_date = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
    elif period == "month":
        start_date = today.replace(day=1).strftime("%Y-%m-%d")
    elif period == "year":
        start_date = today.replace(month=1, day=1).strftime("%Y-%m-%d")
    else:
        await update.message.reply_text("Invalid period. Please use 'day', 'week', 'month', or 'year'.")
        return

    try:
        transactions = get_transactions_in_range(start_date, end_date)
        loop = asyncio.get_running_loop()
        categories_map = await loop.run_in_executor(None, get_categories_from_actual)
        # Invert categories_map to get category name from ID
        category_names_by_id = {v: k for k, v in categories_map.items()}

        if not transactions:
            await update.message.reply_text(f'No spending found for the {period}.')
            return

        response_message = f'Spending for {period.capitalize()} ({start_date} to {end_date}):\n\n'

        if detail_level == "simple":
            spending_by_category = {}
            for t in transactions:
                if t.amount < 0: # Only consider expenses
                    category_name = category_names_by_id.get(t.category, 'Uncategorized')
                    spending_by_category[category_name] = spending_by_category.get(category_name, 0) + abs(t.amount)

            for category, amount in sorted(spending_by_category.items()):
                response_message += f'{category.capitalize()}: ${amount / 100:.2f}\n'
        elif detail_level == "detailed":
            for t in transactions:
                if t.amount < 0: # Only consider expenses
                    description = t.payee or t.notes or 'No description'
                    amount = f"${abs(t.amount / 100):.2f}"
                    category_name = category_names_by_id.get(t.category, 'Uncategorized')
                    response_message += f'- {t.date}: {description} - {amount} ({category_name.capitalize()})\n'
        else:
            await update.message.reply_text("Invalid detail level. Please use 'simple' or 'detailed'.")
            return
        
        await update.message.reply_text(response_message)

    except (ConnectionError, ValueError) as e:
        await update.message.reply_text(f'API Error: {e}')
    except Exception as e:
        await update.message.reply_text(f'Error: {e}')

async def get_ai_suggestion(update, context):
    try:
        # Fetch transactions for the last year
        today = date.today()
        start_date = (today - timedelta(days=365)).strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")

        transactions = get_transactions_in_range(start_date, end_date)

        if not transactions:
            await update.message.reply_text("No transactions found for the last year to analyze for AI suggestions.")
            return

        # Format transactions for Gemini API
        formatted_transactions = []
        for t in transactions:
            formatted_transactions.append({
                "date": t.date,
                "amount": t.amount / 100, # Convert cents to dollars
                "payee": t.payee,
                "category_id": t.category,
                "notes": t.notes
            })
        
        # Convert category IDs to names for better readability for AI
        loop = asyncio.get_running_loop()
        categories_map = await loop.run_in_executor(None, get_categories_from_actual) # Removed actual_token as it's no longer needed
        category_names_by_id = {v: k for k, v in categories_map.items()}
        for t in formatted_transactions:
            t['category_name'] = category_names_by_id.get(t.pop('category_id'), 'Uncategorized')
        

        transactions_json = json.dumps(formatted_transactions, indent=2)

        # Construct prompt for Gemini API
        prompt = (
            "Analyze the following financial transactions from the last year and suggest one simple, actionable savings method. "
            "Focus on a practical tip that can be easily implemented based on the spending patterns observed. "
            "Provide only the suggestion, without any conversational filler.\n\n"
            "Transactions:\n"
            f"{transactions_json}"
        )

        # Call Gemini API
        model = genai.GenerativeModel('gemini-pro') # Using gemini-pro as flash might be too small for detailed analysis
        response = model.generate_content(prompt)
        
        if response and response.text:
            await update.message.reply_text(f'AI Savings Suggestion:\n{response.text}')
        else:
            await update.message.reply_text("Could not get a savings suggestion from AI. Please try again later.")

    except (ConnectionError, ValueError) as e:
        await update.message.reply_text(f'API Error: {e}')
    except Exception as e:
        await update.message.reply_text(f'Error: {e}')

async def sync_bank(update, context):
    try:

        with Actual(base_url=ACTUAL_API_URL, password=ACTUAL_PASSWORD, file=ACTUAL_BUDGET_ID, cert=False) as actual:
            accounts_map = get_accounts_from_actual()
            all_synchronized_transactions = []
            response_message_parts = []

            for account_id, account_name in accounts_map.items():
                try:
                    # Call run_bank_sync for each individual account
                    synchronized_transactions = actual.run_bank_sync(account=account_id)
                    if synchronized_transactions:
                        response_message_parts.append(f"Synchronized transactions for {account_name}:\n")
                        for transaction in synchronized_transactions:
                            response_message_parts.append(f"  Added or modified {transaction.payee.name} - ${abs(transaction.amount / 100):.2f}")
                        all_synchronized_transactions.extend(synchronized_transactions)
                    else:
                        response_message_parts.append(f"No new transactions to synchronize for {account_name}.")
                except Exception as account_e:
                    response_message_parts.append(f"Error syncing {account_name}: {account_e}")
            
            if all_synchronized_transactions:
                actual.commit() # sync changes back to the server
                await update.message.reply_text("\n".join(response_message_parts))
            else:
                await update.message.reply_text("No new transactions to synchronize across all accounts.")
    except MultipleResultsFound as e:
        response_message_parts.append(f"Error syncing {account_name}: {e}. This usually means there are duplicate accounts or ambiguous data in your Actual Budget server for this account. Please check your Actual Budget UI for '{account_name}'.")
    except UnknownFileId as e:
        response_message_parts.append(f"Error syncing {account_name}: {e}. This might indicate an issue with your ACTUAL_BUDGET_ID or multiple budgets with the same name. Please check your Actual Budget server configuration.")
    except ActualError as e:
        response_message_parts.append(f"Error syncing {account_name}: {e}. Please check your Actual Budget server and SimpleFIN configuration for this account.")
    except Exception as e:
        response_message_parts.append(f"An unexpected error occurred while syncing {account_name}: {e}")

async def unrecognized_command(update, context):
    print(f"Unrecognized command from user {update.effective_user.id}: {update.message.text}")
    options_message = (
        "Available commands:\n\n"
        "\"Add\": `add [Payee] [Amount]` (e.g., `add Groceries 20`)\n"
        "\"Sort\": `sort` (categorizes recent uncategorized expense)\n"
        "\"Spending\": `spending [day/week/month/year] [simple/detailed]` (e.g., `spending month simple`)\n"
        "\"AI\": `ai savings` (gets a savings suggestion)\n"
        "\"Sync\": `sync` (runs bank synchronization)"
    )
    await update.message.reply_text(options_message)

def main():
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    print("Starting Budget Bot...")
    application.add_handler(CommandHandler("start", start))
    print("Added /start command handler")
    application.add_handler(CommandHandler("cancel", cancel_flow))
    application.add_handler(CommandHandler("stop", cancel_flow))
    print("Added /cancel and /stop command handlers")
    # Handler for inline keyboard callbacks for sorting
    application.add_handler(CallbackQueryHandler(handle_sort_reply, pattern=r'^sort_category_'))
    # Handler for "Cancel" button in inline keyboard
    application.add_handler(CallbackQueryHandler(cancel_flow, pattern=r'^cancel_sort_flow$'))
    print("Added CallbackQueryHandler for sort categories and cancel button")
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Added general message handler")
    application.run_polling()
    print("Bot is running...")

if __name__ == '__main__':
    main()