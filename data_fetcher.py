import os
from datetime import date, timedelta, datetime
from dotenv import load_dotenv
from actual import Actual
from actual.queries import get_transactions, get_categories as get_categories_from_actual_queries, get_accounts, get_budgets, get_payees

class DataFetcher:
    def __init__(self):
        load_dotenv()
        self.actual_api_url = os.getenv('ACTUAL_API_URL')
        self.actual_budget_id = os.getenv('ACTUAL_BUDGET_ID')
        self.actual_password = os.getenv('ACTUAL_PASSWORD')
        self._budget_cache_by_month = {} # {date(YYYY, MM, 1): [budget_objects]}

        # New cache structures
        self._categories_cache = {}      # {name_lower: id}
        self._category_id_to_name_cache = {} # {id: name}
        self._accounts_cache = {}        # {id: name}
        self._payees_cache = {}          # {name_lower: id}
        self._payee_id_to_name_cache = {} # {id: name}
        self._transactions_cache = []    # List of all fetched transaction objects
        self._last_cache_refresh = None  # Timestamp of last refresh

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

    def _check_cache_staleness(self, refresh_interval_hours: int = 6) -> bool:
        """Checks if the cache is stale and needs a refresh."""
        if not self._last_cache_refresh:
            return True
        time_since_last_refresh = datetime.now() - self._last_cache_refresh
        return time_since_last_refresh > timedelta(hours=refresh_interval_hours)

    async def get_categories(self):
        """Fetches all categories from Actual Budget, using cache if available."""
        print("DEBUG: DataFetcher.get_categories called.")
        if self._check_cache_staleness() or not self._categories_cache:
            await self.refresh_cache_sync()
        return self._categories_cache

    async def get_category_id_to_name_map(self):
        """Fetches a map of category ID to category name, using cache if available."""
        if self._check_cache_staleness() or not self._category_id_to_name_cache:
            await self.refresh_cache_sync()
        return self._category_id_to_name_cache

    async def get_accounts(self):
        """Fetches all accounts from Actual Budget, using cache if available."""
        if self._check_cache_staleness() or not self._accounts_cache:
            await self.refresh_cache_sync()
        return self._accounts_cache

    async def get_payees(self):
        """Fetches all payees from Actual Budget, using cache if available."""
        if self._check_cache_staleness() or not self._payees_cache:
            await self.refresh_cache_sync()
        return self._payees_cache

    async def get_payee_id_to_name_map(self):
        """Fetches a map of payee ID to payee name, using cache if available."""
        if self._check_cache_staleness() or not self._payee_id_to_name_cache:
            await self.refresh_cache_sync()
        return self._payee_id_to_name_cache

    async def get_transactions_in_range(self, start_date: date, end_date: date):
        """
        Fetches transactions within a specified date range, primarily from cache.
        If data is not in cache or cache is stale, it triggers a refresh.
        """
        if self._check_cache_staleness() or not self._transactions_cache:
            await self.refresh_cache_sync()
        
        # Filter transactions from cache based on the date range
        filtered_transactions = []
        for t in self._transactions_cache:
            transaction_date = t.date
            if isinstance(transaction_date, int):
                transaction_date = datetime.strptime(str(transaction_date), "%Y%m%d").date()
            
            if start_date <= transaction_date <= end_date:
                filtered_transactions.append(t)
        return filtered_transactions

    def _get_all_budgets_for_month(self, month: date):
        """
        Fetches all budgeted amounts for a given month.
        Caches the results to avoid multiple API calls for the same month.
        """
        if month not in self._budget_cache_by_month or self._check_cache_staleness():
            # self.refresh_cache_sync() is an async function, so it needs to be awaited.
            # However, this function is now synchronous.
            # The budget cache will be refreshed when refresh_cache_sync is called from an async context.
            with self._get_actual_session() as actual:
                all_budgets = get_budgets(actual.session, month=month)
                self._budget_cache_by_month[month] = all_budgets
        return self._budget_cache_by_month[month]

    async def get_budget_for_category(self, category_id: str, month: date):
        """
        Fetches the budgeted amount for a given category ID in a specific month.
        month should be the first day of the month (e.g., date(2023, 6, 1)).
        Uses cache if available.
        """
        all_budgets = self._get_all_budgets_for_month(month)
        for budget in all_budgets:
            if budget.category and budget.category.id == category_id:
                return budget.get_amount() * 100  # Convert to cents
        return 0

    async def get_spent_for_category_and_month(self, category_id: str, month_date: date):
        """
        Calculates the total spent for a given category ID in a specific month.
        month_date should be the first day of the month (e.g., date(2023, 6, 1)).
        Uses cached transactions.
        """
        next_month = month_date.replace(day=28) + timedelta(days=4)
        end_of_month = next_month - timedelta(days=next_month.day)

        transactions = await self.get_transactions_in_range(month_date, end_of_month)
        
        total_spent = 0
        for t in transactions:
            if t.amount < 0: # Only consider expenses
                if t.category and t.category.id == category_id:
                    total_spent += abs(t.amount)
        return total_spent

    async def get_financial_data(self, categories: list[str], num_months: int) -> str:
        """
        Fetches and formats financial data (transactions, budgets, and spending) for specified categories
        over a given number of past months, suitable for AI analysis.
        Primarily uses cached data.

        Args:
            categories: A list of category names (e.g., ["Groceries", "Eating Out"]). Use ["All"] to include all categories.
            num_months: The number of months back from the current date to fetch data (e.g., 6 for the last 6 months).

        Returns:
            A human-readable string containing formatted financial data.
        """
        print(f"DEBUG: get_financial_data called for categories: {categories}, months: {num_months}")
        if self._check_cache_staleness():
            print("DEBUG: Cache is stale, refreshing...")
            await self.refresh_cache_sync()
        else:
            print("DEBUG: Cache is fresh, using cached data.")

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
        
        all_transactions = await self.get_transactions_in_range(start_date, today)

        formatted_financial_data = await self._format_financial_data_for_gemini(
            all_transactions, categories, num_months
        )
        return formatted_financial_data

    async def _format_financial_data_for_gemini(self, transactions, categories: list[str], num_months: int):
        """
        Formats financial data (transactions and budgets) into a human-readable string
        suitable for Gemini's analysis.
        Handles multiple categories or "all" categories.
        """
        output = []
        
        # Get category name to ID map for easier lookup
        all_categories_map = await self.get_categories() # {name_lower: id}
        category_id_to_name_map = await self.get_category_id_to_name_map()

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
                budgeted_amount = await self.get_budget_for_category(cat_id, month_date)
                output.append(f"  Budget for {cat_name_display}: ${budgeted_amount / 100:.2f}")

                # Get spending for the specific category for this month
                spent_amount = await self.get_spent_for_category_and_month(cat_id, month_date)
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
            current_budget = await self.get_budget_for_category(cat_id, current_month_date)
            current_spent = await self.get_spent_for_category_and_month(cat_id, current_month_date)
            output.append(f"  Budget for {cat_name_display}: ${current_budget / 100:.2f}")
            output.append(f"  Spent on {cat_name_display} so far: ${current_spent / 100:.2f}")
        output.append(f"  It is currently the {today.day}th day of the month.")

        return "\n".join(output)


    async def refresh_cache_sync(self):
        """Asynchronous wrapper for refreshing all cached data from Actual Budget."""
        import asyncio
        # Use asyncio.to_thread to run the async method in a separate thread
        await asyncio.to_thread(self.refresh_cache_async)

    def refresh_cache_async(self):
        """Asynchronously refreshes all cached data from Actual Budget."""
        print("DEBUG: refresh_cache_async called. Refreshing all caches...")
        with self._get_actual_session() as actual:
            # Fetch and cache categories
            categories_data = get_categories_from_actual_queries(actual.session)
            self._categories_cache = {category.name.lower(): category.id for category in categories_data}
            self._category_id_to_name_cache = {category.id: category.name for category in categories_data}
            print(f"DEBUG: Categories cached: {len(self._categories_cache)} items.")

            # Fetch and cache accounts
            accounts_data = get_accounts(actual.session)
            self._accounts_cache = {str(account.id): account.name for account in accounts_data}
            print(f"DEBUG: Accounts cached: {len(self._accounts_cache)} items.")

            # Fetch and cache payees
            payees_data = get_payees(actual.session)
            self._payees_cache = {payee.name.lower(): payee.id for payee in payees_data}
            self._payee_id_to_name_cache = {payee.id: payee.name for payee in payees_data}
            print(f"DEBUG: Payees cached: {len(self._payees_cache)} items.")

            # Fetch and cache transactions for a relevant period (e.g., last 12-24 months)
            today = date.today()
            start_date_for_transactions = today - timedelta(days=365 * 2) # Last 2 years of transactions
            self._transactions_cache = get_transactions(actual.session, start_date=start_date_for_transactions, end_date=today)
            print(f"DEBUG: Transactions cached: {len(self._transactions_cache)} items.")

            # Clear and re-populate budget cache for relevant months
            self._budget_cache_by_month = {}
            for i in range(24): # Cache budgets for the last 24 months
                month_to_cache = (today.replace(day=1) - timedelta(days=30*i)).replace(day=1)
                self._get_all_budgets_for_month(month_to_cache) # This will populate the cache
            print(f"DEBUG: Budget cache populated for {len(self._budget_cache_by_month)} months.")

            self._last_cache_refresh = datetime.now()
            print(f"DEBUG: Cache refresh complete at {self._last_cache_refresh}.")


import asyncio

async def main():
    # Example usage:
    # Make sure to set ACTUAL_API_URL, ACTUAL_BUDGET_ID, ACTUAL_PASSWORD in your .env file
    try:
        fetcher = DataFetcher()
        await fetcher.refresh_cache_sync() # Initial cache refresh

        print("--- Categories ---")
        categories = await fetcher.get_categories()
        print(f"Categories: {categories}")

        print("\n--- Accounts ---")
        accounts = await fetcher.get_accounts()
        print(f"Accounts: {accounts}")

        print("\n--- Payees ---")
        payees = await fetcher.get_payees()
        print(f"Payees: {payees}")

        print("\n--- Transactions for last 30 days ---")
        today = date.today()
        thirty_days_ago = today - timedelta(days=30)
        transactions = await fetcher.get_transactions_in_range(thirty_days_ago, today)
        for t in transactions[:5]: # Print first 5 transactions
            print(f"  {t.date}: {t.payee.name if t.payee else 'N/A'} - ${t.amount / 100:.2f} ({t.category.name if t.category else 'Uncategorized'})")

        print("\n--- Budget and Spent for 'Groceries' in current month ---")
        current_month = today.replace(day=1)
        # Assuming "Groceries" is a category name, need to get its ID
        categories_map = await fetcher.get_categories()
        grocery_category_id = categories_map.get("groceries")
        if grocery_category_id:
            grocery_budget = await fetcher.get_budget_for_category(grocery_category_id, current_month)
            grocery_spent = await fetcher.get_spent_for_category_and_month(grocery_category_id, current_month)
            print(f"Grocery Budget: ${grocery_budget / 100:.2f}")
            print(f"Grocery Spent: ${grocery_spent / 100:.2f}")
        else:
            print("Groceries category not found.")

        print("\n--- Formatted Financial Data for Gemini (Groceries, last 6 months) ---")
        formatted_data = await fetcher.get_financial_data(["Groceries"], 6)
        print(formatted_data)

    except ValueError as e:
        print(f"Configuration Error: {e}")
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    asyncio.run(main())