import telegram
import asyncio
import re
import argparse # Import argparse
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, CallbackQueryHandler, filters
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
import json
import os
from datetime import date, timedelta, datetime
from dotenv import load_dotenv
from actual import Actual
from actual.queries import get_transactions, get_categories as get_categories_from_actual_queries, get_accounts, reconcile_transaction, get_budgets
from actual.exceptions import UnknownFileId, ActualError
from rules_manager import RuleSet, Rule, Condition, Action, ConditionType, ActionType, ValueType, load_rules, save_rules
from sqlalchemy.orm.exc import MultipleResultsFound
from pathlib import Path

from gemini_client import GeminiClient
from data_fetcher import DataFetcher

dotenv_path = Path('./stack.env')
load_dotenv(dotenv_path=dotenv_path) # Load environment variables from stack.env file

# --- Configuration ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
ACTUAL_API_URL = os.environ.get('ACTUAL_API_URL')
ACTUAL_BUDGET_ID = os.environ.get('ACTUAL_BUDGET_ID')
ACTUAL_CASH_ACCOUNT_ID = os.environ.get('ACTUAL_CASH_ACCOUNT_ID')
ACTUAL_PASSWORD = os.environ.get('ACTUAL_PASSWORD')
PUBLIC_DOMAIN = os.environ.get('PUBLIC_DOMAIN')
TELEGRAM_CHAT_ID_RAW = os.environ.get('TELEGRAM_CHAT_ID')
TELEGRAM_CHAT_IDS = [chat_id.strip() for chat_id in TELEGRAM_CHAT_ID_RAW.split(' ')] if TELEGRAM_CHAT_ID_RAW else []

# --- Initialize Clients ---
gemini_client = GeminiClient()
data_fetcher = DataFetcher()

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

def get_category_id_to_name_map():
    with Actual(base_url=ACTUAL_API_URL, password=ACTUAL_PASSWORD, file=ACTUAL_BUDGET_ID, cert=False) as actual:
        categories_data = get_categories_from_actual_queries(actual.session)
        return {category.id: category.name for category in categories_data}

def get_accounts_from_actual():
    with Actual(base_url=ACTUAL_API_URL, password=ACTUAL_PASSWORD, file=ACTUAL_BUDGET_ID, cert=False) as actual:
        accounts_data = get_accounts(actual.session)
        accounts_map = {}
        for account in accounts_data:
            accounts_map[str(account.id)] = account.name
        return accounts_map

def get_uncategorized_transactions(session, start_date=None):
    print(f"Fetching uncategorized transactions from {start_date.strftime('%Y-%m-%d') if start_date else 'all time'}...")
    try:
        all_transactions = get_transactions(session, start_date=start_date)
        uncategorized_transactions = [t for t in all_transactions if t.category is None]
    except Exception as e:
        print(f"Error fetching transactions: {e}")
        raise ConnectionError(f"Error fetching transactions: {e}")
    print(f"Found {len(uncategorized_transactions)} uncategorized transactions.")
    return uncategorized_transactions

def get_transactions_in_range(session, start_date, end_date):
    transactions = get_transactions(session, start_date=start_date, end_date=end_date)
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

    if context.user_data.get('awaiting_days_input'):
        await handle_days_input(update, context)
        return
    elif context.user_data.get('awaiting_months_input'):
        await handle_months_input(update, context)
        return
    elif context.user_data.get('awaiting_years_input'):
        await handle_years_input(update, context)
        return
    elif context.user_data.get('awaiting_rule_operation'):
        await handle_rule_operation_input(update, context)
        return
    elif context.user_data.get('awaiting_condition_field'):
        await handle_condition_field_input(update, context)
        return
    elif context.user_data.get('awaiting_condition_op'):
        await handle_condition_op_input(update, context)
        return
    elif context.user_data.get('awaiting_condition_value'):
        await handle_condition_value_input(update, context)
        return
    elif context.user_data.get('awaiting_action_field'):
        await handle_action_field_input(update, context)
        return
    elif context.user_data.get('awaiting_action_op'):
        await handle_action_op_input(update, context)
        return
    elif context.user_data.get('awaiting_action_value'):
        await handle_action_value_input(update, context)
        return
    elif context.user_data.get('awaiting_ai_months') and context.user_data.get('ai_custom_months_input'):
        await handle_ai_months_input(update, context)
        return
    elif context.user_data.get('awaiting_ai_question'):
        await handle_ai_question_input(update, context)
        return

    # The bot will now primarily rely on CommandHandler for these.
    # This handler will catch messages that are not commands.
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
                    'Assign Category:',
                    f'Description: {description or "N/A"}',
                    f'Account: {account_name}',
                    f'Payee: {latest_transaction.payee.name or "N/A"}',
                    f'Amount: ${abs(latest_transaction.amount / 100):.2f}',
                    f'Date: {datetime.strptime(str(latest_transaction.date), "%Y%m%d").strftime("%A, %m/%d/%Y")}',
                    f'Link: {PUBLIC_DOMAIN}/transactions/{latest_transaction.id}' if latest_transaction.id else 'N/A'
                ]
                if "amazon" in description.lower() or "amazon" in latest_transaction.payee.name.lower():
                    message_parts.append("Amazon Link: https://www.amazon.com/your-orders ")
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
                            
                            # Get transaction date and month for budget comparison
                            transaction_date = datetime.strptime(str(transaction_to_update.get_date()), "%Y-%m-%d").date()
                            transaction_month = transaction_date.replace(day=1) # First day of the month
                            
                            # Get spent and budget for the assigned category and month
                            spent_amount = await get_spent_for_category_and_month(actual.session, category_name_lower, transaction_month)
                            budgeted_amount = await get_budget_for_category(actual.session, category_name_lower, transaction_month)
                            emoji = get_budget_emoji(spent_amount, budgeted_amount)
                            month_name = transaction_date.strftime("%B")
                            response_text = (
                                f'Expense categorized as {category_name_title_case}.\n'
                                f'{month_name} {category_name_title_case}: '
                                f'${spent_amount / 100:.2f}/${budgeted_amount / 100:.2f} {emoji}'
                            )
                            await query.edit_message_text(response_text)
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
                                
                                # Get transaction date and month for budget comparison
                                transaction_date = datetime.strptime(str(transaction_to_update.get_date()), "%Y-%m-%d").date()
                                transaction_month = transaction_date.replace(day=1) # First day of the month

                                # Get spent and budget for the assigned category and month
                                spent_amount = await get_spent_for_category_and_month(actual.session, category_name_lower, transaction_month)
                                budgeted_amount = await get_budget_for_category(actual.session, category_name_lower, transaction_month)
                                emoji = get_budget_emoji(spent_amount, budgeted_amount)

                                month_name = transaction_date.strftime("%B")
                                response_text = (
                                    f'Expense categorized as {category_name_title_case}.\n'
                                    f'{month_name} {category_name_title_case}: '
                                    f'${spent_amount / 100:.2f}/${budgeted_amount / 100:.2f} {emoji}'
                                )
                                await update.message.reply_text(response_text)
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

async def cancel_flow(update, context, silent=False):
    print(f"DEBUG: Entering cancel_flow. Silent: {silent}")
    context.user_data.pop('awaiting_category_for_sort', None)
    context.user_data.pop('sorting_transaction', None)
    context.user_data.pop('sorting_in_progress', None)
    
    # Clear rule creation state
    context.user_data.pop('creating_rule', None)
    context.user_data.pop('current_rule', None)
    context.user_data.pop('awaiting_rule_operation', None)
    context.user_data.pop('awaiting_condition_field', None)
    context.user_data.pop('awaiting_condition_op', None)
    context.user_data.pop('awaiting_condition_value', None)
    context.user_data.pop('awaiting_action_field', None)
    context.user_data.pop('awaiting_action_op', None)
    context.user_data.pop('awaiting_action_value', None)

    # Clear spending flow state
    context.user_data.pop('awaiting_days_input', None)
    context.user_data.pop('awaiting_months_input', None)
    context.user_data.pop('awaiting_years_input', None)

    # Clear AI flow state
    context.user_data.pop('awaiting_ai_categories', None)
    context.user_data.pop('ai_selected_categories', None)
    context.user_data.pop('awaiting_ai_months', None)
    context.user_data.pop('ai_num_months', None)
    context.user_data.pop('awaiting_ai_question', None)
    context.user_data.pop('ai_custom_months_input', None)

    if not silent:
        if update.callback_query:
            print("DEBUG: cancel_flow: Handling callback query.")
            await update.callback_query.answer()
            await update.callback_query.edit_message_text("Flow cancelled.")
        else:
            print("DEBUG: cancel_flow: Handling message.")
            await update.message.reply_text("Flow cancelled.")
    print("DEBUG: Exiting cancel_flow.")

async def get_category_selection_keyboard(context):
    loop = asyncio.get_running_loop()
    categories_dict = await loop.run_in_executor(None, get_categories_from_actual)
    
    keyboard = []
    row = []
    # Add "All Categories" option
    keyboard.append([InlineKeyboardButton("All Categories", callback_data="ai_category_all")])

    for i, category_name in enumerate(sorted(categories_dict.keys())):
        button_text = category_name.title()
        if category_name.lower() in context.user_data.get('ai_selected_categories', []):
            button_text = "✅ " + button_text
        button = InlineKeyboardButton(button_text, callback_data=f"ai_category_{category_name.lower()}")
        row.append(button)
        if (i + 1) % 3 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    
    # Add a "Done" button to proceed after selection
    keyboard.append([InlineKeyboardButton("Done", callback_data="ai_category_done")])
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_flow")])

    return InlineKeyboardMarkup(keyboard)

async def get_months_back_keyboard():
    keyboard = [
        [InlineKeyboardButton("1 Month", callback_data="ai_months_1"),
         InlineKeyboardButton("3 Months", callback_data="ai_months_3"),
         InlineKeyboardButton("6 Months", callback_data="ai_months_6")],
        [InlineKeyboardButton("12 Months", callback_data="ai_months_12"),
         InlineKeyboardButton("Custom", callback_data="ai_months_custom")],
        [InlineKeyboardButton("Cancel", callback_data="cancel_flow")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def get_spending(update, context):
    keyboard = [
        [InlineKeyboardButton("Spent", callback_data="spending_spent")],
        [InlineKeyboardButton("Trajectory (Coming Soon)", callback_data="spending_trajectory")],
        [InlineKeyboardButton("Alerts (Coming Soon)", callback_data="spending_alerts")],
        [InlineKeyboardButton("Cancel", callback_data="cancel_flow")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("What spending information would you like to see?", reply_markup=reply_markup)

async def ai_command(update, context):
    """Initiates the AI financial analysis flow."""
    context.user_data['ai_selected_categories'] = []
    await update.message.reply_text("Please select categories for AI analysis (you can select multiple, or 'All'):",
                                    reply_markup=await get_category_selection_keyboard(context))
    context.user_data['awaiting_ai_categories'] = True

async def handle_ai_category_selection(update, context):
    query = update.callback_query
    await query.answer()
    callback_data = query.data

    if callback_data == "ai_category_all":
        loop = asyncio.get_running_loop()
        categories_dict = await loop.run_in_executor(None, get_categories_from_actual)
        context.user_data['ai_selected_categories'] = list(categories_dict.keys())
        await query.edit_message_text("All categories selected. Now, how many months back?",
                                      reply_markup=await get_months_back_keyboard())
        context.user_data['awaiting_ai_categories'] = False
        context.user_data['awaiting_ai_months'] = True
    elif callback_data == "ai_category_done": # Handle 'Done' button first
        if not context.user_data['ai_selected_categories']:
            error_message_text = "Please select at least one category, or 'All Categories'."
            if query.message.text != error_message_text:
                await query.edit_message_text(error_message_text)
            return
        
        # Show a "processing" state with a timer emoji
        await query.edit_message_text("Processing categories... ⏳")
        await asyncio.sleep(0.5) # Add a small delay to allow the message to update

        new_message_text = (f"Categories selected: {', '.join([c.title() for c in context.user_data['ai_selected_categories']])}.\n"
                            f"Now, how many months back?")
        new_reply_markup = await get_months_back_keyboard()
        
        # Always edit the message after the "processing" state to show the next step
        await query.edit_message_text(new_message_text, reply_markup=new_reply_markup)
        
        context.user_data['awaiting_ai_categories'] = False
        context.user_data['awaiting_ai_months'] = True
    elif callback_data.startswith("ai_category_"): # Handle individual category selection last
        category_name = callback_data.replace("ai_category_", "")
        if category_name not in context.user_data['ai_selected_categories']:
            context.user_data['ai_selected_categories'].append(category_name)
        else:
            context.user_data['ai_selected_categories'].remove(category_name)
        
        new_reply_markup = await get_category_selection_keyboard(context)
        current_reply_markup = query.message.reply_markup

        # Compare the new reply_markup with the current one to avoid BadRequest error
        if json.dumps(new_reply_markup.to_dict()) != json.dumps(current_reply_markup.to_dict()):
            await query.edit_message_text(f"Please select categories for AI analysis (you can select multiple, or 'All'):",
                                          reply_markup=new_reply_markup)
    elif callback_data == "cancel_flow":
        await cancel_flow(query, context)

async def handle_ai_months_selection(update, context):
    query = update.callback_query
    await query.answer()
    callback_data = query.data

    if callback_data.startswith("ai_months_"):
        months_str = callback_data.replace("ai_months_", "")
        if months_str == "custom":
            await query.edit_message_text("Please enter the number of months back for AI analysis:")
            context.user_data['awaiting_ai_months'] = True # Keep this true to catch the next message
            context.user_data['ai_custom_months_input'] = True
        else:
            num_months = int(months_str)
            context.user_data['ai_num_months'] = num_months
            await query.edit_message_text(f"Months back set to {num_months}. Now, please type your question for the AI:")
            context.user_data['awaiting_ai_months'] = False
            context.user_data['awaiting_ai_question'] = True
    elif callback_data == "cancel_flow":
        await cancel_flow(query, context)

async def handle_ai_months_input(update, context):
    try:
        num_months = int(update.message.text)
        if num_months <= 0:
            await update.message.reply_text("Please provide a positive number of months.")
            return
        context.user_data['ai_num_months'] = num_months
        await update.message.reply_text(f"Months back set to {num_months}. Now, please type your question for the AI:")
        context.user_data['awaiting_ai_months'] = False
        context.user_data.pop('ai_custom_months_input', None)
        context.user_data['awaiting_ai_question'] = True
    except ValueError:
        await update.message.reply_text("Invalid input for number of months. Please use a number.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def handle_ai_question_input(update, context):
    user_question = update.message.text
    categories = context.user_data.get('ai_selected_categories', [])
    num_months = context.user_data.get('ai_num_months')

    if not categories or num_months is None:
        await update.message.reply_text("Error: Categories or number of months not set. Please restart with /ai.")
        await cancel_flow(update, context, silent=True)
        return

    await update.message.reply_text(f"Analyzing for categories: {', '.join([c.title() for c in categories])} for the last {num_months} months with AI. Please wait...")

    today = date.today()
    year = today.year
    month = today.month
    
    for _ in range(num_months - 1):
        if month == 1:
            month = 12
            year -= 1
        else:
            month -= 1
    
    start_date = date(year, month, 1)
    
    all_transactions = data_fetcher.get_transactions_in_range(start_date, today)

    formatted_financial_data = data_fetcher.format_financial_data_for_gemini(
        all_transactions, categories, num_months
    )

    full_prompt_raw = (
        f"The user asked: '{user_question}'\n\n"
        "Here is the relevant financial data:\n"
        f"{formatted_financial_data}\n\n"
        "Please analyze this data and answer the user's question. "
        "Provide a concise and actionable answer based on the provided data. "
        "Please respond in plain text without any special formatting. No markdown formatting."
        "Repsond to the user in a chatbot friendly way, but straightforward and concise."
    )
    full_prompt = re.sub(r"\s+", ' ', full_prompt_raw).strip()

    gemini_responses = gemini_client.send_prompt(full_prompt)

    if gemini_responses:
        for response_chunk in gemini_responses:
            await update.message.reply_text(response_chunk)
    else:
        await update.message.reply_text("Could not get a response from AI. Please try again later.")

    # Clear AI flow state
    context.user_data.pop('awaiting_ai_categories', None)
    context.user_data.pop('ai_selected_categories', None)
    context.user_data.pop('awaiting_ai_months', None)
    context.user_data.pop('ai_num_months', None)
    context.user_data.pop('awaiting_ai_question', None)
    context.user_data.pop('ai_custom_months_input', None)

async def sync_bank_logic():
    """
    Performs the bank synchronization logic.
    Returns a tuple: (success_message, error_message)
    """
    all_synchronized_transactions = []
    response_message_parts = []
    error_message = None

    try:
        with Actual(base_url=ACTUAL_API_URL, password=ACTUAL_PASSWORD, file=ACTUAL_BUDGET_ID, cert=False) as actual:
            accounts_map = get_accounts_from_actual()

            for account_id, account_name in accounts_map.items():
                try:
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
                actual.commit()
                success_message = "\n".join(response_message_parts)
            else:
                success_message = "No new transactions to synchronize across all accounts."
            
            return success_message, None # No error

    except MultipleResultsFound as e:
        error_message = f"Error syncing: {e}. This usually means there are duplicate accounts or ambiguous data in your Actual Budget server. Please check your Actual Budget UI."
    except UnknownFileId as e:
        error_message = f"Error syncing: {e}. This might indicate an issue with your ACTUAL_BUDGET_ID or multiple budgets with the same name. Please check your Actual Budget server configuration."
    except ActualError as e:
        error_message = f"Error syncing: {e}. Please check your Actual Budget server and SimpleFIN configuration."
    except Exception as e:
        error_message = f"An unexpected error occurred while syncing: {e}"
    
    return None, error_message

async def sync_command_handler(update, context):
    """Handles the /sync command from a user."""
    await update.message.reply_text("Starting bank synchronization...")
    success_message, error_message = await sync_bank_logic()
    if success_message:
        await update.message.reply_text(success_message)
    elif error_message:
        await update.message.reply_text(f"Synchronization failed: {error_message}")
    else:
        await update.message.reply_text("Synchronization completed with an unknown result.")

async def unrecognized_command(update, context):
    print(f"Unrecognized command from user {update.effective_user.id}: {update.message.text}")
    await update.message.reply_text("Unrecognized command. Please use the menu button or type a valid command.")

async def send_notification(application, chat_ids, message):
    for chat_id in chat_ids:
        try:
            await application.bot.send_message(chat_id=chat_id, text=message)
            print(f"Notification sent to chat ID {chat_id}: {message}")
        except Exception as e:
            print(f"Error sending notification to chat ID {chat_id}: {e}")

async def scheduled_sync_and_notify(application):
    while True:
        print("Running scheduled sync and notification...")
        try:
            sync_success_message, sync_error_message = await sync_bank_logic()

            if sync_error_message:
                print(f"Scheduled sync failed: {sync_error_message}")
                if TELEGRAM_CHAT_IDS:
                    await send_notification(application, TELEGRAM_CHAT_IDS, f"Scheduled bank sync failed: {sync_error_message}")
            else:
                print(f"Scheduled sync completed: {sync_success_message}")
                today = date.today()
                thirty_days_ago = today - timedelta(days=30)
                
                with Actual(base_url=ACTUAL_API_URL, password=ACTUAL_PASSWORD, file=ACTUAL_BUDGET_ID, cert=False) as actual:
                    uncategorized_count = len(get_uncategorized_transactions(actual.session, start_date=thirty_days_ago))
                
                if uncategorized_count > 0:
                    notification_message = f"Sync complete! Found {uncategorized_count} new uncategorized transactions in the last 30 days. Use /sort to categorize them."
                else:
                    notification_message = "Sync complete! No new uncategorized transactions found in the last 30 days."

                budget_comparison_message = await get_monthly_budget_comparison_message()
                notification_message += f"\n{budget_comparison_message}"

                if TELEGRAM_CHAT_IDS:
                    await send_notification(application, TELEGRAM_CHAT_IDS, notification_message)
                else:
                    print("TELEGRAM_CHAT_IDS not set. Cannot send notification.")

        except Exception as e:
            print(f"Error in scheduled sync and notification: {e}")
        
        await asyncio.sleep(24 * 60 * 60) # Wait for 24 hours

async def set_bot_commands(application):
    commands = [
        telegram.BotCommand("sort", "Sort uncategorized expenses"),
        telegram.BotCommand("add", "Add a new expense (e.g., add Groceries 20)"),
        telegram.BotCommand("spending", "View spending summary"),
        telegram.BotCommand("ai", "Ask AI for financial analysis"),
        telegram.BotCommand("sync", "Synchronize bank accounts"),
        telegram.BotCommand("rules", "Manage rules for transactions"),
        telegram.BotCommand("readrules", "Get list of existing rules"),
        telegram.BotCommand("createrule", "Walk through implementation of a new rule"),
        telegram.BotCommand("runrules", "Runs all rules on all transactions")
    ]
    await application.bot.set_my_commands(commands)
    print("Bot commands set successfully.")

async def post_init_callback(application, args):
    await set_bot_commands(application)
    if not args.no_sync:
        asyncio.create_task(scheduled_sync_and_notify(application))
    else:
        print("Scheduled sync and notifications bypassed due to --no-sync flag.")

async def send_long_message_in_chunks(message, text, chunk_size=4096):
    """Sends a long message by splitting it into chunks."""
    for i in range(0, len(text), chunk_size):
        await message.reply_text(text[i:i + chunk_size])
async def handle_spending_spent_callback(update, context):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("Day", callback_data="spending_day")],
        [InlineKeyboardButton("Days", callback_data="spending_days")],
        [InlineKeyboardButton("Month", callback_data="spending_month")],
        [InlineKeyboardButton("Months", callback_data="spending_months")],
        [InlineKeyboardButton("Year", callback_data="spending_year")],
        [InlineKeyboardButton("Years", callback_data="spending_years")],
        [InlineKeyboardButton("Cancel", callback_data="cancel_flow")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("Select a spending period:", reply_markup=reply_markup)

async def handle_spending_trajectory_callback(update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Trajectory feature is coming soon!")
    await cancel_flow(query, context, silent=True)

async def handle_spending_alerts_callback(update, context):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Alerts feature is coming soon!")
    await cancel_flow(query, context, silent=True)

async def get_budget_for_category(session, category_name, month):
    try:
        # get_budgets returns a list, even if only one budget is found for a category and month
        budgets = get_budgets(session, month=month, category=category_name.title())
        if budgets:
            # Assuming we only care about the first budget found for a given category and month
            # Multiply by 100 to convert to cents, assuming get_amount() returns dollars or a pre-divided value
            return budgets[0].get_amount() * 100
        return 0 # No budget set for this category and month
    except Exception as e:
        print(f"Error fetching budget for category {category_name} in {month}: {e}")
        return 0

async def get_spent_for_category_and_month(session, category_name_lower, month_date):
    """
    Calculates the total spent for a given category in a specific month.
    month_date should be the first day of the month (e.g., date(2023, 6, 1)).
    """
    try:
        # Get all transactions for the specified month
        # Need to determine the end date of the month
        next_month = month_date.replace(day=28) + timedelta(days=4) # Advance to next month
        end_of_month = next_month - timedelta(days=next_month.day) # Go back to last day of previous month

        transactions = get_transactions_in_range(session, month_date, end_of_month)
        
        loop = asyncio.get_running_loop()
        categories_map = await loop.run_in_executor(None, get_categories_from_actual)
        category_names_by_id = {v: k for k, v in categories_map.items()}

        total_spent = 0
        for t in transactions:
            if t.amount < 0: # Only consider expenses
                category_of_transaction = category_names_by_id.get(t.category.id, 'Uncategorized') if t.category else 'Uncategorized'
                if category_of_transaction.lower() == category_name_lower:
                    total_spent += abs(t.amount)
        return total_spent
    except Exception as e:
        print(f"Error calculating spent for category {category_name_lower} in {month_date}: {e}")
        return 0

def get_budget_emoji(spent_amount, budgeted_amount):
    if budgeted_amount == 0:
        return "" # No budget, no emoji
    if spent_amount <= budgeted_amount:
        return "✅" # Within budget
    else:
        return "❌" # Over budget

async def calculate_spending_for_period(message, context, start_date, end_date, period_name, detail_level="simple", num_months=1):
    try:
        with Actual(base_url=ACTUAL_API_URL, password=ACTUAL_PASSWORD, file=ACTUAL_BUDGET_ID, cert=False) as actual:
            transactions = get_transactions_in_range(actual.session, start_date, end_date)
            
            loop = asyncio.get_running_loop()
            categories_map = await loop.run_in_executor(None, get_categories_from_actual)
            category_names_by_id = {v: k for k, v in categories_map.items()}

            if not transactions:
                await message.reply_text(f'No spending found for the {period_name}.')
                return

            response_message = f'Spending for {period_name.capitalize()} ({start_date} to {end_date}):\n\n'

            if detail_level == "simple":
                spending_by_category = {}
                for t in transactions:
                    if t.amount < 0: # Only consider expenses
                        category_name = category_names_by_id.get(t.category.id, 'Uncategorized') if t.category else 'Uncategorized'
                        spending_by_category[category_name] = spending_by_category.get(category_name, 0) + abs(t.amount)

                # Fetch budgets for the current month (or relevant month for multi-month/day views)
                # For 'days' and 'months' commands, we still use the monthly budget as a base
                budget_month = date.today().replace(day=1) # Always use the current month's budget as a base

                budget_data = {}
                for category_name in spending_by_category.keys():
                    budget_amount = await get_budget_for_category(actual.session, category_name, budget_month)
                    budget_data[category_name] = budget_amount * num_months # Scale budget by number of months

                for category, spent_amount in sorted(spending_by_category.items()):
                    budgeted_amount = budget_data.get(category, 0)
                    emoji = get_budget_emoji(spent_amount, budgeted_amount)
                    response_message += f'{category.capitalize()}: Spent ${spent_amount / 100:.2f} / Budgeted ${budgeted_amount / 100:.2f} {emoji}\n'
            elif detail_level == "detailed":
                for t in transactions:
                    if t.amount < 0: # Only consider expenses
                        description = t.payee or t.notes or 'No description'
                        amount = f"${abs(t.amount / 100):.2f}"
                        category_name = category_names_by_id.get(t.category.id, 'Uncategorized') if t.category else 'Uncategorized'
                        response_message += f'- {t.date}: {description} - {amount} ({category_name.capitalize()})\n'
            else:
                await message.reply_text("Invalid detail level. Please use 'simple' or 'detailed'.")
                return
            
            await send_long_message_in_chunks(message, response_message)

    except (ConnectionError, ValueError) as e:
        await message.reply_text(f'API Error: {e}')
    except Exception as e:
        await message.reply_text(f'Error: {e}')

async def get_monthly_budget_comparison_message():
    today = date.today()
    start_of_month = today.replace(day=1)
    current_day_of_month = today.day
    days_in_month = (date(today.year, today.month % 12 + 1, 1) - timedelta(days=1)).day

    try:
        with Actual(base_url=ACTUAL_API_URL, password=ACTUAL_PASSWORD, file=ACTUAL_BUDGET_ID, cert=False) as actual:
            transactions = get_transactions_in_range(actual.session, start_of_month, today)
            
            loop = asyncio.get_running_loop()
            categories_map = await loop.run_in_executor(None, get_categories_from_actual)
            category_names_by_id = {v: k for k, v in categories_map.items()}

            spending_by_category = {}
            for t in transactions:
                if t.amount < 0: # Only consider expenses
                    category_name = category_names_by_id.get(t.category.id, 'Uncategorized') if t.category else 'Uncategorized'
                    spending_by_category[category_name] = spending_by_category.get(category_name, 0) + abs(t.amount)

            budget_month = start_of_month
            budget_comparison_data = []

            for category, spent_amount in spending_by_category.items():
                budgeted_amount = await get_budget_for_category(actual.session, category, budget_month)
                
                percent_spent = 0
                if budgeted_amount > 0:
                    percent_spent = (spent_amount / budgeted_amount) * 100
                elif spent_amount > 0: # Spent money but no budget
                    percent_spent = 1000000 # Effectively infinite over budget

                budget_comparison_data.append({
                    "category": category,
                    "spent": spent_amount,
                    "budget": budgeted_amount,
                    "percent_spent": percent_spent
                })
            
            # Sort by percent_spent (highest percent spent to lowest)
            budget_comparison_data.sort(key=lambda x: x['percent_spent'], reverse=True)

            message_parts = ["\n--- Monthly Budget Comparison ---"]
            if not budget_comparison_data:
                message_parts.append("No spending data for the current month.")
            else:
                for item in budget_comparison_data:
                    category = item['category'].capitalize()
                    spent = item['spent'] / 100
                    budget = item['budget'] / 100
                    percent_spent = item['percent_spent']

                    status_emoji = ""
                    if percent_spent > 100:
                        status_emoji = "❌" # Over budget
                    elif percent_spent <= 100:
                        status_emoji = "✅" # Within budget

                    message_parts.append(
                        f"{category}: Spent ${spent:.2f} | Budget ${budget:.2f} | "
                        f"{percent_spent:.2f}% of budget {status_emoji}"
                    )
            return "\n".join(message_parts)

    except (ConnectionError, ValueError) as e:
        return f"API Error fetching budget comparison: {e}"
    except Exception as e:
        return f"Error generating budget comparison: {e}"

async def handle_spending_day_callback(update, context):
    query = update.callback_query
    await query.answer()
    today = date.today()
    await calculate_spending_for_period(query.message, context, today, today, "current day")
    await cancel_flow(query, context, silent=True)

async def handle_spending_days_callback(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_days_input'] = True
    await query.edit_message_text("How many days back would you like to see spending for? (e.g., 7 for last 7 days)")

async def handle_days_input(update, context):
    try:
        days_back = int(update.message.text)
        if days_back <= 0:
            await update.message.reply_text("Please enter a positive number of days.")
            return
        
        today = date.today()
        start_date = today - timedelta(days=days_back - 1)
        await calculate_spending_for_period(update.message, context, start_date, today, f"last {days_back} days")
        context.user_data.pop('awaiting_days_input', None)
        await cancel_flow(update, context, silent=True)
    except ValueError:
        await update.message.reply_text("Invalid input. Please enter a number.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def handle_spending_month_callback(update, context):
    query = update.callback_query
    await query.answer()
    today = date.today()
    start_date = today.replace(day=1)
    await calculate_spending_for_period(query.message, context, start_date, today, "current month")
    await cancel_flow(query, context, silent=True)

async def handle_spending_months_callback(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_months_input'] = True
    await query.edit_message_text("How many months back would you like to see spending for? (e.g., 3 for last 3 months)")

async def handle_months_input(update, context):
    try:
        months_back = int(update.message.text)
        if months_back <= 0:
            await update.message.reply_text("Please provide a positive number of months.")
            return
        
        today = date.today()
        # Calculate start date for X months back
        year = today.year
        month = today.month
        
        # Go back months_back - 1 full months, then set day to 1
        for _ in range(months_back - 1):
            if month == 1:
                month = 12
                year -= 1
            else:
                month -= 1
        
        start_date = date(year, month, 1)
        await calculate_spending_for_period(update.message, context, start_date, today, f"last {months_back} months", num_months=months_back)
        context.user_data.pop('awaiting_months_input', None)
        await cancel_flow(update, context, silent=True)
    except ValueError:
        await update.message.reply_text("Invalid input. Please enter a number.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def handle_spending_year_callback(update, context):
    query = update.callback_query
    await query.answer()
    today = date.today()
    start_date = today.replace(month=1, day=1)
    await calculate_spending_for_period(query.message, context, start_date, today, "current year", num_months=12)
    await cancel_flow(query, context, silent=True)

async def handle_spending_years_callback(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data['awaiting_years_input'] = True
    await query.edit_message_text("How many years back would you like to see spending for? (e.g., 2 for last 2 years)")

async def handle_years_input(update, context):
    try:
        years_back = int(update.message.text)
        if years_back <= 0:
            await update.message.reply_text("Please enter a positive number of years.")
            return
        
        today = date.today()
        start_date = today.replace(year=today.year - years_back + 1, month=1, day=1)
        await calculate_spending_for_period(update.message, context, start_date, today, f"last {years_back} years", num_months=years_back * 12)
        context.user_data.pop('awaiting_years_input', None)
        await cancel_flow(update, context, silent=True)
    except ValueError:
        await update.message.reply_text("Invalid input. Please enter a number.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

# --- Rules Command Handlers (Placeholders) ---
async def rules_menu(update, context):
    await update.message.reply_text(
        "Welcome to the Rules Management! Here are the available commands:\n"
        "/readrules - Get a list of existing rules\n"
        "/createrule - Walk through the implementation of a new rule\n"
        "/runrules - Runs all rules on all transactions"
    )

async def read_rules(update, context):
    await update.message.reply_text("Fetching existing rules...")
    try:
        loop = asyncio.get_running_loop()
        category_id_to_name_map = await loop.run_in_executor(None, get_category_id_to_name_map)
        print(f"DEBUG: In read_rules, category_id_to_name_map: {category_id_to_name_map}")
        ruleset = load_rules(category_id_to_name_map)
        if ruleset.rules:
            response_message = "Existing Rules:\n\n"
            for i, rule in enumerate(ruleset.rules):
                response_message += f"Rule {i+1}:\n{rule}\n\n"
            await update.message.reply_text(response_message)
        else:
            await update.message.reply_text("No rules found. Use /createrule to add a new rule.")
    except Exception as e:
        await update.message.reply_text(f"Error reading rules: {e}")

async def create_rule_start(update, context):
    await update.message.reply_text("Let's create a new rule! What kind of operation should the rule use for its conditions? (Type 'and' or 'or')")
    context.user_data['creating_rule'] = True
    context.user_data['current_rule'] = {'conditions': [], 'actions': []}
    context.user_data['awaiting_rule_operation'] = True
    
    keyboard = [
        [InlineKeyboardButton("AND (all conditions must match)", callback_data="rule_op_and")],
        [InlineKeyboardButton("OR (any condition can match)", callback_data="rule_op_or")],
        [InlineKeyboardButton("Cancel", callback_data="cancel_rule_flow")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Let's create a new rule! What kind of operation should the rule use for its conditions?",
        reply_markup=reply_markup
    )

async def handle_rule_operation_input(update, context):
    text = update.message.text.lower()
    if text in ["and", "or"]:
        context.user_data['current_rule']['operation'] = text
        context.user_data['awaiting_rule_operation'] = False
        await ask_for_condition_field(update, context)
    else:
        await update.message.reply_text("Invalid operation. Please type 'and' or 'or'.")

async def handle_rule_operation_callback(update, context):
    query = update.callback_query
    await query.answer()
    callback_data = query.data
    if callback_data == "rule_op_and":
        context.user_data['current_rule']['operation'] = "and"
        context.user_data['awaiting_rule_operation'] = False
        await query.edit_message_text("Rule operation set to AND. Now, let's add conditions.")
        await ask_for_condition_field(query.message, context)
    elif callback_data == "rule_op_or":
        context.user_data['current_rule']['operation'] = "or"
        context.user_data['awaiting_rule_operation'] = False
        await query.edit_message_text("Rule operation set to OR. Now, let's add conditions.")
        await ask_for_condition_field(query.message, context)
    elif callback_data == "cancel_rule_flow":
        await cancel_flow(query, context)

async def ask_for_condition_field(update, context):
    context.user_data['awaiting_condition_field'] = True
    keyboard = [
        [InlineKeyboardButton("Description", callback_data="condition_field_description")],
        [InlineKeyboardButton("Notes", callback_data="condition_field_notes")],
        [InlineKeyboardButton("Amount", callback_data="condition_field_amount")],
        [InlineKeyboardButton("Category", callback_data="condition_field_category")],
        [InlineKeyboardButton("Account", callback_data="condition_field_acct")],
        [InlineKeyboardButton("Date", callback_data="condition_field_date")],
        [InlineKeyboardButton("Imported Description", callback_data="condition_field_imported_description")],
        [InlineKeyboardButton("Cancel", callback_data="cancel_rule_flow")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.reply_text("What field do you want to set a condition on?", reply_markup=reply_markup)

async def handle_condition_field_input(update, context):
    field = update.message.text.lower()
    valid_fields = [f.value for f in Condition.__fields__.get('field').type.__args__] # Accessing Literal values
    if field in valid_fields:
        context.user_data['current_condition_field'] = field
        context.user_data['awaiting_condition_field'] = False
        await ask_for_condition_op(update, context)
    else:
        await update.message.reply_text(f"Invalid field. Please choose from: {', '.join(valid_fields)}")

async def handle_condition_field_callback(update, context):
    query = update.callback_query
    await query.answer()
    field = query.data.replace("condition_field_", "")
    context.user_data['current_condition_field'] = field
    context.user_data['awaiting_condition_field'] = False
    await query.edit_message_text(f"Condition field set to '{field}'. Now, choose an operation.")
    await ask_for_condition_op(query.message, context)

async def ask_for_condition_op(update, context):
    context.user_data['awaiting_condition_op'] = True
    field_type = ValueType.from_field(context.user_data['current_condition_field'])
    
    keyboard = []
    row = []
    for i, op_type in enumerate(ConditionType):
        if field_type.is_valid(op_type):
            button = InlineKeyboardButton(op_type.value.replace("_", " ").title(), callback_data=f"condition_op_{op_type.value}")
            row.append(button)
            if (i + 1) % 3 == 0: # 3 buttons per row
                keyboard.append(row)
                row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_rule_flow")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.reply_text("What operation do you want to use?", reply_markup=reply_markup)

async def handle_condition_op_input(update, context):
    op_str = update.message.text.lower().replace(" ", "")
    try:
        op = ConditionType(op_str)
        field_type = ValueType.from_field(context.user_data['current_condition_field'])
        if field_type.is_valid(op):
            context.user_data['current_condition_op'] = op
            context.user_data['awaiting_condition_op'] = False
            await ask_for_condition_value(update, context)
        else:
            await update.message.reply_text(f"Operation '{op_str}' is not valid for field type '{field_type.name}'. Please choose a valid operation.")
    except ValueError:
        await update.message.reply_text("Invalid operation. Please choose a valid operation from the list.")

async def handle_condition_op_callback(update, context):
    query = update.callback_query
    await query.answer()
    op_str = query.data.replace("condition_op_", "")
    op = ConditionType(op_str)
    context.user_data['current_condition_op'] = op
    context.user_data['awaiting_condition_op'] = False
    await query.edit_message_text(f"Condition operation set to '{op_str}'. Now, enter the value.")
    await ask_for_condition_value(query.message, context)

async def ask_for_condition_value(update, context):
    context.user_data['awaiting_condition_value'] = True
    await update.reply_text("Please enter the value for the condition:")

async def handle_condition_value_input(update, context):
    value_str = update.message.text
    field = context.user_data['current_condition_field']
    op = context.user_data['current_condition_op']
    
    try:
        value_type = ValueType.from_field(field)
        # Attempt to convert value based on type
        if value_type == ValueType.NUMBER:
            value = int(float(value_str) * 100) # Convert to cents
        elif value_type == ValueType.BOOLEAN:
            value = value_str.lower() == 'true'
        elif value_type == ValueType.DATE:
            value = date.fromisoformat(value_str)
        else:
            value = value_str

        new_condition = Condition(field=field, op=op, value=value)
        context.user_data['current_rule']['conditions'].append(new_condition.model_dump(mode="json"))
        context.user_data['awaiting_condition_value'] = False
        await add_another_condition(update, context)

    except ValueError as e:
        await update.message.reply_text(f"Invalid value for type '{value_type.name}': {e}. Please try again.")
    except Exception as e:
        await update.message.reply_text(f"Error processing value: {e}. Please try again.")

async def add_another_condition(update, context):
    keyboard = [
        [InlineKeyboardButton("Add another condition", callback_data="add_another_condition")],
        [InlineKeyboardButton("Continue to actions", callback_data="continue_to_actions")],
        [InlineKeyboardButton("Cancel", callback_data="cancel_rule_flow")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Condition added. What's next?", reply_markup=reply_markup)

async def handle_add_another_condition_callback(update, context):
    query = update.callback_query
    await query.answer()
    callback_data = query.data
    if callback_data == "add_another_condition":
        await query.edit_message_text("Adding another condition.")
        await ask_for_condition_field(query.message, context)
    elif callback_data == "continue_to_actions":
        await query.edit_message_text("Continuing to actions.")
        await ask_for_action_field(query.message, context)
    elif callback_data == "cancel_rule_flow":
        await cancel_flow(query, context)

async def ask_for_action_field(update, context):
    context.user_data['awaiting_action_field'] = True
    keyboard = [
        [InlineKeyboardButton("Category", callback_data="action_field_category")],
        [InlineKeyboardButton("Description", callback_data="action_field_description")],
        [InlineKeyboardButton("Notes", callback_data="action_field_notes")],
        [InlineKeyboardButton("Cleared", callback_data="action_field_cleared")],
        [InlineKeyboardButton("Account", callback_data="action_field_acct")],
        [InlineKeyboardButton("Date", callback_data="action_field_date")],
        [InlineKeyboardButton("Amount", callback_data="action_field_amount")],
        [InlineKeyboardButton("Cancel", callback_data="cancel_rule_flow")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.reply_text("What field do you want to apply an action to?", reply_markup=reply_markup)

async def handle_action_field_input(update, context):
    field = update.message.text.lower()
    valid_fields = [f.value for f in Action.__fields__.get('field').type.__args__[0].__args__] # Accessing Literal values
    if field in valid_fields:
        context.user_data['current_action_field'] = field
        context.user_data['awaiting_action_field'] = False
        await ask_for_action_op(update, context)
    else:
        await update.message.reply_text(f"Invalid field. Please choose from: {', '.join(valid_fields)}")

async def handle_action_field_callback(update, context):
    query = update.callback_query
    await query.answer()
    field = query.data.replace("action_field_", "")
    context.user_data['current_action_field'] = field
    context.user_data['awaiting_action_field'] = False
    await query.edit_message_text(f"Action field set to '{field}'. Now, choose an operation.")
    await ask_for_action_op(query.message, context)

async def ask_for_action_op(update, context):
    context.user_data['awaiting_action_op'] = True
    keyboard = []
    row = []
    for i, op_type in enumerate(ActionType):
        button = InlineKeyboardButton(op_type.value.replace("_", " ").title(), callback_data=f"action_op_{op_type.value}")
        row.append(button)
        if (i + 1) % 3 == 0: # 3 buttons per row
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel_rule_flow")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.reply_text("What action operation do you want to use?", reply_markup=reply_markup)

async def handle_action_op_input(update, context):
    op_str = update.message.text.lower().replace(" ", "")
    try:
        op = ActionType(op_str)
        context.user_data['current_action_op'] = op
        context.user_data['awaiting_action_op'] = False
        await ask_for_action_value(update, context)
    except ValueError:
        await update.message.reply_text("Invalid action operation. Please choose a valid operation.")

async def handle_action_op_callback(update, context):
    query = update.callback_query
    await query.answer()
    op_str = query.data.replace("action_op_", "")
    op = ActionType(op_str)
    context.user_data['current_action_op'] = op
    context.user_data['awaiting_action_op'] = False
    await query.edit_message_text(f"Action operation set to '{op_str}'. Now, enter the value.")
    await ask_for_action_value(query.message, context)

async def ask_for_action_value(update, context):
    context.user_data['awaiting_action_value'] = True
    await update.reply_text("Please enter the value for the action:")

async def handle_action_value_input(update, context):
    value_str = update.message.text
    field = context.user_data['current_action_field']
    op = context.user_data['current_action_op']

    try:
        # Determine value type based on field for Action
        if field == 'category' or field == 'description' or field == 'acct':
            # For category, description (payee), and account, we need to get the ID
            # This will require fetching categories/accounts from Actual
            loop = asyncio.get_running_loop()
            if field == 'category':
                categories = await loop.run_in_executor(None, get_categories_from_actual)
                value = categories.get(value_str.lower())
                if not value:
                    await update.message.reply_text(f"Category '{value_str}' not found. Please enter a valid category name.")
                    return
            elif field == 'acct':
                accounts = await loop.run_in_executor(None, get_accounts_from_actual)
                # Assuming accounts_map is {id: name}, we need to find id by name
                account_id = next((aid for aid, aname in accounts.items() if aname.lower() == value_str.lower()), None)
                value = account_id
                if not value:
                    await update.message.reply_text(f"Account '{value_str}' not found. Please enter a valid account name.")
                    return
            else: # description (payee)
                # For payee, we might need a way to get payee ID from name, or just use the name directly if actualpy handles it.
                # For now, let's assume direct string value for description field in action.
                value = value_str
        elif field == 'amount':
            value = int(float(value_str) * 100) # Convert to cents
        elif field == 'cleared':
            value = value_str.lower() == 'true'
        elif field == 'date':
            value = date.fromisoformat(value_str)
        else: # notes, or other string fields
            value = value_str

        new_action = Action(field=field, op=op, value=value)
        context.user_data['current_rule']['actions'].append(new_action.model_dump(mode="json"))
        context.user_data['awaiting_action_value'] = False
        await add_another_action(update, context)

    except ValueError as e:
        await update.message.reply_text(f"Invalid value for action: {e}. Please try again.")
    except Exception as e:
        await update.message.reply_text(f"Error processing action value: {e}. Please try again.")

async def add_another_action(update, context):
    keyboard = [
        [InlineKeyboardButton("Add another action", callback_data="add_another_action")],
        [InlineKeyboardButton("Finish rule creation", callback_data="finish_rule_creation")],
        [InlineKeyboardButton("Cancel", callback_data="cancel_rule_flow")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Action added. What's next?", reply_markup=reply_markup)

async def handle_add_another_action_callback(update, context):
    query = update.callback_query
    await query.answer()
    callback_data = query.data
    if callback_data == "add_another_action":
        await query.edit_message_text("Adding another action.")
        await ask_for_action_field(query.message, context)
    elif callback_data == "finish_rule_creation":
        await query.edit_message_text("Finishing rule creation.")
        await finish_rule_creation(query.message, context)
    elif callback_data == "cancel_rule_flow":
        await cancel_flow(query, context)

async def finish_rule_creation(update, context):
    try:
        rule_data = context.user_data['current_rule']
        new_rule = Rule(**rule_data)
        
        ruleset = load_rules()
        ruleset.add(new_rule)
        save_rules(ruleset)

        await update.reply_text("Rule created and saved successfully!")
        context.user_data.pop('creating_rule', None)
        context.user_data.pop('current_rule', None)
    except Exception as e:
        await update.reply_text(f"Error finishing rule creation: {e}")
        # Optionally, clear partial rule data on error
        context.user_data.pop('creating_rule', None)
        context.user_data.pop('current_rule', None)

async def run_rules(update, context):
    await update.message.reply_text("Running all rules on all transactions...")
    try:
        ruleset = load_rules()
        if not ruleset.rules:
            await update.message.reply_text("No rules defined to run. Use /createrule to add rules.")
            return

        with Actual(base_url=ACTUAL_API_URL, password=ACTUAL_PASSWORD, file=ACTUAL_BUDGET_ID, cert=False) as actual:
            all_transactions = get_transactions(actual.session)
            
            transactions_affected_count = 0
            for transaction in all_transactions:
                # Create a temporary object to pass to rule.run that mimics the transaction structure
                # This is a workaround because the Rule.run in rules_manager.py is simplified
                # and doesn't directly interact with SQLAlchemy objects.
                # In a real scenario, Rule.run would be part of actualpy or designed to work with its objects.
                temp_transaction_data = {
                    "id": transaction.id,
                    "date": transaction.get_date(),
                    "amount": transaction.get_amount(),
                    "payee": transaction.payee.name if transaction.payee else None,
                    "notes": transaction.notes,
                    "category": transaction.category,
                    "account": transaction.account.id,
                    "cleared": bool(transaction.cleared),
                    "imported_description": transaction.imported_description
                }
                
                # Store original values to check for changes
                original_notes = temp_transaction_data["notes"]
                original_category = temp_transaction_data["category"]
                original_amount = temp_transaction_data["amount"]
                original_cleared = temp_transaction_data["cleared"]

                rule_applied = False
                for rule in ruleset.rules:
                    # Pass a mutable dictionary that rule.run can modify
                    if rule.run(temp_transaction_data):
                        rule_applied = True
                
                if rule_applied:
                    # Apply changes back to the actual SQLAlchemy transaction object
                    # This is a simplified update. A more robust solution would use reconcile_transaction
                    # or similar Actual.py methods for proper updates and change tracking.
                    
                    # Check if notes changed
                    if temp_transaction_data["notes"] != original_notes:
                        transaction.notes = temp_transaction_data["notes"]
                    
                    # Check if category changed
                    if temp_transaction_data["category"] != original_category:
                        transaction.category = temp_transaction_data["category"]
                    
                    # Check if amount changed (only if ActionType.SET was used for amount)
                    if temp_transaction_data["amount"] != original_amount:
                        transaction.amount = temp_transaction_data["amount"]

                    # Check if cleared status changed
                    if temp_transaction_data["cleared"] != original_cleared:
                        transaction.cleared = temp_transaction_data["cleared"]

                    # For other fields, you would add similar checks and updates.
                    # For now, we'll assume direct attribute assignment is sufficient for simple cases.
                    
                    transactions_affected_count += 1
            
            if transactions_affected_count > 0:
                actual.commit() # Commit all changes made by rules
                await update.message.reply_text(f"Successfully ran rules. {transactions_affected_count} transactions were affected.")
            else:
                await update.message.reply_text("Rules ran, but no transactions were affected.")

    except Exception as e:
        await update.message.reply_text(f"Error running rules: {e}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Budget Bot for Telegram.")
    parser.add_argument('--no-sync', action='store_true', help='Bypass scheduled bank synchronization and notifications.')
    args = parser.parse_args()

    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(lambda app: post_init_callback(app, args)).build()
    print("Starting Budget Bot...")
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel_flow))
    application.add_handler(CommandHandler("stop", cancel_flow))
    application.add_handler(CommandHandler("sort", sort_expense))
    application.add_handler(CommandHandler("add", add_expense))
    application.add_handler(CommandHandler("spending", get_spending))
    application.add_handler(CommandHandler("ai", ai_command)) # New handler for AI analysis
    application.add_handler(CommandHandler("sync", sync_command_handler))
    application.add_handler(CommandHandler("categories", get_categories))
    application.add_handler(CommandHandler("rules", rules_menu)) # New handler for the main rules command
    application.add_handler(CommandHandler("readrules", read_rules))
    application.add_handler(CommandHandler("createrule", create_rule_start))
    application.add_handler(CommandHandler("runrules", run_rules))
    print("Added command handlers for start, cancel, stop, sort, add, spending, ai, sync, categories, and rules commands.")

    application.add_handler(CallbackQueryHandler(handle_sort_reply, pattern=r'^sort_category_'))
    application.add_handler(CallbackQueryHandler(cancel_flow, pattern=r'^cancel_sort_flow$'))
    
    # New CallbackQueryHandlers for rule creation
    application.add_handler(CallbackQueryHandler(handle_rule_operation_callback, pattern=r'^rule_op_'))
    application.add_handler(CallbackQueryHandler(handle_condition_field_callback, pattern=r'^condition_field_'))
    application.add_handler(CallbackQueryHandler(handle_condition_op_callback, pattern=r'^condition_op_'))
    application.add_handler(CallbackQueryHandler(handle_add_another_condition_callback, pattern=r'^(add_another_condition|continue_to_actions|cancel_rule_flow)$'))
    application.add_handler(CallbackQueryHandler(handle_action_field_callback, pattern=r'^action_field_'))
    application.add_handler(CallbackQueryHandler(handle_action_op_callback, pattern=r'^action_op_'))
    application.add_handler(CallbackQueryHandler(handle_add_another_action_callback, pattern=r'^(add_another_action|finish_rule_creation|cancel_rule_flow)$'))
    application.add_handler(CallbackQueryHandler(cancel_flow, pattern=r'^cancel_rule_flow$'))

    # New CallbackQueryHandlers for spending flow
    application.add_handler(CallbackQueryHandler(handle_spending_spent_callback, pattern=r'^spending_spent$'))
    application.add_handler(CallbackQueryHandler(handle_spending_trajectory_callback, pattern=r'^spending_trajectory$'))
    application.add_handler(CallbackQueryHandler(handle_spending_alerts_callback, pattern=r'^spending_alerts$'))
    application.add_handler(CallbackQueryHandler(handle_spending_day_callback, pattern=r'^spending_day$'))
    application.add_handler(CallbackQueryHandler(handle_spending_days_callback, pattern=r'^spending_days$'))
    application.add_handler(CallbackQueryHandler(handle_spending_month_callback, pattern=r'^spending_month$'))
    application.add_handler(CallbackQueryHandler(handle_spending_months_callback, pattern=r'^spending_months$'))
    application.add_handler(CallbackQueryHandler(handle_spending_year_callback, pattern=r'^spending_year$'))
    application.add_handler(CallbackQueryHandler(handle_spending_years_callback, pattern=r'^spending_years$'))

    # New CallbackQueryHandlers for AI flow
    application.add_handler(CallbackQueryHandler(handle_ai_category_selection, pattern=r'^ai_category_'))
    application.add_handler(CallbackQueryHandler(handle_ai_months_selection, pattern=r'^ai_months_'))

    print("Added CallbackQueryHandler for sort categories, cancel button, rule creation flow, spending flow, and AI flow.")
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("Added general message handler for unrecognized commands.")
    

    application.run_polling()
    print("Bot is running...")
