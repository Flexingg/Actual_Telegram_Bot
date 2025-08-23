import os
from datetime import date, timedelta, datetime
from dotenv import load_dotenv
from actual import Actual
from actual.queries import get_transactions, get_categories as get_categories_from_actual_queries, get_accounts, get_budgets

class DataFetcher:
    def __init__(self):
        load_dotenv()
        self.actual_api_url = os.getenv('ACTUAL_API_URL')
        self.actual_budget_id = os.getenv('ACTUAL_BUDGET_ID')
        self.actual_password = os.getenv('ACTUAL_PASSWORD')
        self._budget_cache_by_month = {} # Cache for all budgets for a given month

        if not all([self.actual_api_url, self.actual_budget_id, self.actual_password]):
            raise ValueError("Actual Budget API credentials (ACTUAL_API_URL, ACTUAL_BUDGET_ID, ACTUAL_PASSWORD) not found in environment variables.")

    def _get_actual_session(self):
        """Helper to get an Actual session."""
        return Actual(
            base_url=self.actual_api_url,
            password=self.actual_password,
            file=self.actual_budget_id,
            cert=False
        )

    def get_categories(self):
        """Fetches all categories from Actual Budget."""
        with self._get_actual_session() as actual:
            categories_data = get_categories_from_actual_queries(actual.session)
            return {category.name.lower(): category.id for category in categories_data}

    def get_category_id_to_name_map(self):
        """Fetches a map of category ID to category name."""
        with self._get_actual_session() as actual:
            categories_data = get_categories_from_actual_queries(actual.session)
            return {category.id: category.name for category in categories_data}

    def get_accounts(self):
        """Fetches all accounts from Actual Budget."""
        with self._get_actual_session() as actual:
            accounts_data = get_accounts(actual.session)
            accounts_map = {}
            for account in accounts_data:
                accounts_map[str(account.id)] = account.name
            return accounts_map

    def get_transactions_in_range(self, start_date: date, end_date: date):
        """Fetches transactions within a specified date range."""
        with self._get_actual_session() as actual:
            transactions = get_transactions(actual.session, start_date=start_date, end_date=end_date)
            return transactions

    def _get_all_budgets_for_month(self, month: date):
        """
        Fetches all budgeted amounts for a given month.
        Caches the results to avoid multiple API calls for the same month.
        """
        if month not in self._budget_cache_by_month:
            with self._get_actual_session() as actual:
                # Fetch all budgets for the month without specifying a category
                all_budgets = get_budgets(actual.session, month=month)
                self._budget_cache_by_month[month] = all_budgets
        return self._budget_cache_by_month[month]

    def get_budget_for_category(self, category_id: str, month: date):
        """
        Fetches the budgeted amount for a given category ID in a specific month.
        month should be the first day of the month (e.g., date(2023, 6, 1)).
        """
        all_budgets = self._get_all_budgets_for_month(month)
        for budget in all_budgets:
            if budget.category and budget.category.id == category_id:
                return budget.get_amount() * 100  # Convert to cents
        return 0

    def get_spent_for_category_and_month(self, category_id: str, month_date: date):
        """
        Calculates the total spent for a given category ID in a specific month.
        month_date should be the first day of the month (e.g., date(2023, 6, 1)).
        """
        with self._get_actual_session() as actual:
            next_month = month_date.replace(day=28) + timedelta(days=4)
            end_of_month = next_month - timedelta(days=next_month.day)

            transactions = self.get_transactions_in_range(month_date, end_of_month)
            
            total_spent = 0
            for t in transactions:
                if t.amount < 0: # Only consider expenses
                    if t.category and t.category.id == category_id:
                        total_spent += abs(t.amount)
            return total_spent

    def format_financial_data_for_gemini(self, transactions, categories: list[str], num_months: int):
        """
        Formats financial data (transactions and budgets) into a human-readable string
        suitable for Gemini's analysis.
        Handles multiple categories or "all" categories.
        """
        output = []
        
        # Get category name to ID map for easier lookup
        all_categories_map = self.get_categories() # {name_lower: id}
        category_id_to_name_map = {v: k for k, v in all_categories_map.items()}

        # Determine which categories to include and get their IDs
        selected_categories_lower = []
        selected_category_ids = []
        if "all" in [c.lower() for c in categories]:
            selected_categories_lower = list(all_categories_map.keys())
            selected_category_ids = list(all_categories_map.values())
            categories_display_name = "All Categories"
        else:
            for cat_name in categories:
                cat_name_lower = cat_name.lower()
                if cat_name_lower in all_categories_map:
                    selected_categories_lower.append(cat_name_lower)
                    selected_category_ids.append(all_categories_map[cat_name_lower])
            categories_display_name = ", ".join([c.title() for c in selected_categories_lower])

        # Group transactions by month and category
        monthly_data = {} # { (year, month): { category_id: [transactions] } }
        for t in transactions:
            if t.amount < 0: # Only consider expenses
                if isinstance(t.date, int):
                    transaction_date = datetime.strptime(str(t.date), "%Y%m%d").date()
                else:
                    transaction_date = t.date
                
                transaction_month = transaction_date.replace(day=1)
                cat_id = t.category.id if t.category else None
                
                if cat_id in selected_category_ids:
                    if (transaction_month.year, transaction_month.month) not in monthly_data:
                        monthly_data[(transaction_month.year, transaction_month.month)] = {}
                    if cat_id not in monthly_data[(transaction_month.year, transaction_month.month)]:
                        monthly_data[(transaction_month.year, transaction_month.month)][cat_id] = []
                    
                    monthly_data[(transaction_month.year, transaction_month.month)][cat_id].append(t)

        # Sort months chronologically
        sorted_months = sorted(monthly_data.keys())

        output.append(f"Financial Data for '{categories_display_name}' over the last {num_months} months:\n")

        for year, month in sorted_months:
            month_date = date(year, month, 1)
            month_name = month_date.strftime("%B")
            output.append(f"- {month_name} {year}:")

            for cat_id in selected_category_ids:
                cat_name_display = category_id_to_name_map.get(cat_id, 'Uncategorized').title()

                # Get budget for the specific category for this month
                budgeted_amount = self.get_budget_for_category(cat_id, month_date)
                output.append(f"  Budget for {cat_name_display}: ${budgeted_amount / 100:.2f}")

                # Get spending for the specific category for this month
                spent_amount = self.get_spent_for_category_and_month(cat_id, month_date)
                output.append(f"  Spent on {cat_name_display}: ${spent_amount / 100:.2f}")

                # List individual transactions for the category
                if cat_id in monthly_data[(year, month)]:
                    output.append(f"  Transactions for {cat_name_display}:")
                    for t in monthly_data[(year, month)][cat_id]:
                        output.append(f"    - {t.date}: {t.payee.name if t.payee else 'N/A'} - ${abs(t.amount) / 100:.2f} ({t.notes or 'No notes'})")
                else:
                    output.append(f"  No transactions found for {cat_name_display} this month.")
                output.append("") # Add a blank line for readability

        # Add current month's budget and spending if applicable
        today = date.today()
        current_month_date = today.replace(day=1)
        current_month_name = current_month_date.strftime("%B")
        
        output.append(f"Current Month ({current_month_name} {today.year}):")
        for cat_id in selected_category_ids:
            cat_name_display = category_id_to_name_map.get(cat_id, 'Uncategorized').title()
            current_budget = self.get_budget_for_category(cat_id, current_month_date)
            current_spent = self.get_spent_for_category_and_month(cat_id, current_month_date)
            output.append(f"  Budget for {cat_name_display}: ${current_budget / 100:.2f}")
            output.append(f"  Spent on {cat_name_display} so far: ${current_spent / 100:.2f}")
        output.append(f"  It is currently the {today.day}th day of the month.")

        return "\n".join(output)


if __name__ == "__main__":
    # Example usage:
    # Make sure to set ACTUAL_API_URL, ACTUAL_BUDGET_ID, ACTUAL_PASSWORD in your .env file
    try:
        fetcher = DataFetcher()
        
        print("--- Categories ---")
        categories = fetcher.get_categories()
        print(f"Categories: {categories}")

        print("\n--- Accounts ---")
        accounts = fetcher.get_accounts()
        print(f"Accounts: {accounts}")

        print("\n--- Transactions for last 30 days ---")
        today = date.today()
        thirty_days_ago = today - timedelta(days=30)
        transactions = fetcher.get_transactions_in_range(thirty_days_ago, today)
        for t in transactions[:5]: # Print first 5 transactions
            print(f"  {t.date}: {t.payee.name if t.payee else 'N/A'} - ${t.amount / 100:.2f} ({t.category.name if t.category else 'Uncategorized'})")

        print("\n--- Budget and Spent for 'Groceries' in current month ---")
        current_month = today.replace(day=1)
        grocery_budget = fetcher.get_budget_for_category("Groceries", current_month)
        grocery_spent = fetcher.get_spent_for_category_and_month("groceries", current_month)
        print(f"Grocery Budget: ${grocery_budget / 100:.2f}")
        print(f"Grocery Spent: ${grocery_spent / 100:.2f}")

        print("\n--- Formatted Financial Data for Gemini (Groceries, last 6 months) ---")
        six_months_ago = today - timedelta(days=6 * 30) # Approximate 6 months
        all_transactions_past_6_months = fetcher.get_transactions_in_range(six_months_ago, today)
        formatted_data = fetcher.format_financial_data_for_gemini(all_transactions_past_6_months, "Groceries", 6)
        print(formatted_data)

    except ValueError as e:
        print(f"Configuration Error: {e}")
    except Exception as e:
        print(f"An error occurred: {e}")