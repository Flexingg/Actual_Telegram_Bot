# Plan for Caching Actual Budget Data for AI/Gemini

This document outlines a plan to cache Actual Budget data for read-only access by AI/Gemini, leveraging the Actual Budget API and the `actualpy` library. The cached data will be used to provide quicker responses for AI queries, where the most recent data is not always critical. This caching mechanism will strictly adhere to read-only operations and will not interfere with existing write operations such as `/sort` and `/sync`.

## 1. Existing Initialization and Data Fetching

The `budget_bot.py` and `data_fetcher.py` modules already handle the initialization and connection to the Actual Budget server using the `actualpy` library.

*   **`DataFetcher` Class**: The `DataFetcher` class in `data_fetcher.py` is responsible for:
    *   Loading Actual Budget API credentials from environment variables.
    *   Establishing an `Actual` session.
    *   Providing methods to fetch categories (`get_categories`, `get_category_id_to_name_map`), accounts (`get_accounts`), transactions within a range (`get_transactions_in_range`), and budget information (`get_budget_for_category`, `get_spent_for_category_and_month`).
    *   It already includes a basic in-memory cache for budget data by month (`_budget_cache_by_month`).
    *   The `get_financial_data` method already formats data for Gemini.

## 2. Enhanced Data Caching Mechanism

The existing `DataFetcher` will be extended to maintain a more comprehensive in-memory cache of all relevant Actual Budget data. This will minimize repeated API calls for frequently accessed read-only information.

The cache will store:

*   **All Categories**: A dictionary mapping category names (lowercase) to their IDs, and another mapping IDs to names.
*   **All Accounts**: A dictionary mapping account IDs to their names.
*   **All Payees**: A dictionary mapping payee names (lowercase) to their IDs, and another mapping IDs to names.
*   **Budget Data**: The existing `_budget_cache_by_month` will continue to store budget entries per category per month.
*   **Transactions**: A cache for transactions, potentially indexed by month or a broader time range, to support quick lookups for AI queries. Given that `get_transactions_in_range` is already used, a simple list of all transactions fetched during a refresh could suffice, or a more granular cache by month/year.

**Proposed Cache Structure within `DataFetcher`:**

```python
class DataFetcher:
    def __init__(self):
        # Existing initialization for API credentials
        self.actual_api_url = os.getenv('ACTUAL_API_URL')
        self.actual_budget_id = os.getenv('ACTUAL_BUDGET_ID')
        self.actual_password = os.getenv('ACTUAL_PASSWORD')

        # Existing budget cache
        self._budget_cache_by_month = {} # {date(YYYY, MM, 1): [budget_objects]}

        # New cache structures
        self._categories_cache = {}      # {name_lower: id}
        self._category_id_to_name_cache = {} # {id: name}
        self._accounts_cache = {}        # {id: name}
        self._payees_cache = {}          # {name_lower: id}
        self._payee_id_to_name_cache = {} # {id: name}
        self._transactions_cache = []    # List of all fetched transaction objects
        self._last_cache_refresh = None  # Timestamp of last refresh
```

## 3. Data Fetching and Cache Population

A new method, `refresh_cache()`, will be added to `DataFetcher` to populate and update all cache components. This method will utilize the existing `actualpy` queries.

```python
    def refresh_cache(self):
        """Refreshes all cached data from Actual Budget."""
        with self._get_actual_session() as actual:
            # Fetch and cache categories
            categories_data = get_categories_from_actual_queries(actual.session)
            self._categories_cache = {category.name.lower(): category.id for category in categories_data}
            self._category_id_to_name_cache = {category.id: category.name for category in categories_data}

            # Fetch and cache accounts
            accounts_data = get_accounts(actual.session)
            self._accounts_cache = {str(account.id): account.name for account in accounts_data}

            # Fetch and cache payees
            # Note: actualpy.queries.get_payees is not available, using actual.client.payees.get_payees()
            payees_data = actual.client.payees.get_payees()
            self._payees_cache = {payee['name'].lower(): payee['id'] for payee in payees_data}
            self._payee_id_to_name_cache = {payee['id']: payee['name'] for payee in payees_data}

            # Fetch and cache transactions for a relevant period (e.g., last 12-24 months)
            # This period can be configured based on AI's typical query range
            today = date.today()
            start_date_for_transactions = today - timedelta(days=365 * 2) # Last 2 years of transactions
            self._transactions_cache = self.get_transactions_in_range(start_date_for_transactions, today)

            # Clear and re-populate budget cache for relevant months
            self._budget_cache_by_month = {}
            # Iterate through a range of months to pre-populate budget cache
            for i in range(24): # Cache budgets for the last 24 months
                month_to_cache = (today.replace(day=1) - timedelta(days=30*i)).replace(day=1)
                self._get_all_budgets_for_month(month_to_cache) # This will populate the cache

            self._last_cache_refresh = datetime.now()
```

Existing `DataFetcher` methods like `get_categories`, `get_accounts`, `get_category_id_to_name_map`, `get_budget_for_category`, and `get_spent_for_category_and_month` will be updated to first check the cache before making an API call. If the data is not in the cache or the cache is stale, they will trigger a refresh or fetch the specific data.

## 4. Cache Refresh Strategy

To keep the cached data reasonably fresh, a scheduled refresh mechanism will be implemented.

*   **Scheduled Refresh**: The `refresh_cache()` method will be called periodically (e.g., every 6-12 hours) by a background task within the bot. This can be integrated into the existing `scheduled_sync_and_notify` or a new dedicated task.
*   **On-Demand Refresh**: A mechanism to manually trigger a cache refresh (e.g., an admin command) can be added for immediate updates if needed.
*   **Staleness Check**: Methods accessing the cache can include a simple check for `_last_cache_refresh` to determine if a refresh is needed before serving potentially stale data.

## 5. Read-Only Enforcement

The caching mechanism will strictly enforce read-only access to the Actual Budget data.

*   The `refresh_cache()` method and all cache-accessing methods within `DataFetcher` will *only* use `get` operations from the Actual Budget API and `actualpy` queries.
*   No `addTransactions`, `updateTransaction`, `deleteTransaction`, or any other `create`, `update`, or `delete` operations will be performed by the caching component.
*   The existing `/sort` and `/sync` functionalities, which involve writing to the database, will remain separate and untouched by this caching layer.

## 6. Integration with AI/Gemini

The `gemini_client.py` will be updated to utilize the `DataFetcher`'s enhanced cached data.

*   **Direct Cache Access**: The `GeminiClient` will be able to directly query the `DataFetcher`'s cached data for information like categories, accounts, payees, and historical transactions/budgets.
*   **`get_financial_data` Enhancement**: The `get_financial_data` method in `DataFetcher` will be updated to primarily use the internal cache, falling back to API calls only if necessary (e.g., for data outside the cached range or if the cache is empty/stale). This will significantly speed up AI queries.

This refactored plan leverages existing components and focuses on building a robust, read-only caching layer within `DataFetcher` to optimize AI/Gemini interactions with Actual Budget data.

This plan ensures that AI/Gemini can quickly access budget information for conversational purposes while maintaining the integrity and security of the Actual Budget database by strictly adhering to read-only principles for the caching mechanism.