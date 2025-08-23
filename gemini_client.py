import os
import google.generativeai as genai
from dotenv import load_dotenv

class GeminiClient:
    def __init__(self):
        load_dotenv()
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash") # Default to gemini-flash if not specified

        if not self.api_key:
            raise ValueError("GEMINI_API_KEY not found in environment variables.")

        genai.configure(api_key=self.api_key)
        self.model = genai.GenerativeModel(self.model_name)
        self.max_telegram_message_length = 4096 # Telegram message character limit
        self.max_gemini_input_length = 50000 # A conservative estimate for input prompt length

    def _split_message(self, message: str) -> list[str]:
        """
        Splits a message into chunks that fit within Telegram's message length limit.
        """
        if len(message) <= self.max_telegram_message_length:
            return [message]

        chunks = []
        current_chunk = ""
        words = message.split(' ')

        for word in words:
            if len(current_chunk) + len(word) + 1 <= self.max_telegram_message_length:
                current_chunk += (word + " ")
            else:
                chunks.append(current_chunk.strip())
                current_chunk = (word + " ")
        if current_chunk:
            chunks.append(current_chunk.strip())
        return chunks

    def send_prompt(self, prompt: str) -> list[str]:
        """
        Sends a prompt to the Gemini model and returns the generated text,
        split into multiple messages if necessary.
        """
        print(prompt[0:1000])
        print(len(prompt))
        if len(prompt) > self.max_gemini_input_length:
            return [f"Error: Input prompt exceeds the maximum allowed length of {self.max_gemini_input_length} characters."]

        try:
            response = self.model.generate_content(prompt)
            return self._split_message(response.text)
        except Exception as e:
            print(f"Error sending prompt to Gemini: {e}")
            return [f"Error: Could not get a response from Gemini. {e}"]

if __name__ == "__main__":
    gemini_client = GeminiClient()
    
    # Example 1: Simple query
    print("--- Example 1: Simple Query ---")
    responses1 = gemini_client.send_prompt("What is the capital of France?")
    for i, res in enumerate(responses1):
        print(f"Gemini Part {i+1}: {res}")

    # Example 2: Financial query (simulated)
    print("\n--- Example 2: Financial Query (Simulated) ---")
    financial_prompt = """
    User asked: "Am I on track to make my grocery bill this month?"
    Here is the data for the past 6 months of grocery transactions and budget:
    - January: Budget $300, Spent $280 (4 trips: $70, $80, $60, $70)
    - February: Budget $300, Spent $320 (5 trips: $60, $70, $80, $50, $60)
    - March: Budget $300, Spent $290 (4 trips: $75, $65, $80, $70)
    - April: Budget $300, Spent $310 (5 trips: $60, $60, $70, $60, $60)
    - May: Budget $300, Spent $270 (3 trips: $90, $90, $90)
    - June: Budget $300, Spent $300 (4 trips: $75, $75, $75, $75)

    Current month (July): Budget $300
    Current spending: $100 (2 trips: $50, $50)
    It is currently the middle of July.

    Based on this data, am I on track to make my grocery bill this month?
    """
    responses2 = gemini_client.send_prompt(financial_prompt)
    for i, res in enumerate(responses2):
        print(f"Gemini Part {i+1}: {res}")

    # Example 3: Long prompt (simulated)
    print("\n--- Example 3: Long Prompt (Simulated) ---")
    long_prompt = "a" * 12000 # This will exceed the max_gemini_input_length
    responses3 = gemini_client.send_prompt(long_prompt)
    for i, res in enumerate(responses3):
        print(f"Gemini Part {i+1}: {res}")