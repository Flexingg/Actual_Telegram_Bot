import os
import google.generativeai as genai
from google.generativeai.protos import FunctionCall
from typing import Optional, Union
from dotenv import load_dotenv
from data_fetcher import DataFetcher # Import DataFetcher
 
class GeminiClient:
    def __init__(self, data_fetcher: DataFetcher):
        load_dotenv()
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash") # Default to gemini-flash if not specified
        self.data_fetcher = data_fetcher # Store DataFetcher instance
 
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY not found in environment variables.")
 
        genai.configure(api_key=self.api_key)
        
        # Register relevant DataFetcher methods as tools
        self.model = genai.GenerativeModel(
            self.model_name,
            tools=[
                self.data_fetcher.get_financial_data,
                self.data_fetcher.refresh_cache_sync # Register refresh_cache_sync as a tool
            ]
        )
        self.chat = self.model.start_chat() # Start a chat session for multi-turn conversations
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
 
    def send_message(self, message: str, tool_response: Optional[str] = None, tool_function_name: Optional[str] = None) -> Union[list[str], FunctionCall]:
        """
        Sends a message to the Gemini model and returns the generated text,
        split into multiple messages if necessary, or a FunctionCall if a tool is invoked.
        """
        print(f"Sending message to Gemini: {message[0:500]}...")
        
        try:
            if tool_response and tool_function_name:
                # Send the tool response back to Gemini
                response = self.chat.send_message(
                    genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(
                            name=tool_function_name,
                            response={"result": tool_response}
                        )
                    )
                )
            else:
                # Send the user's message
                response = self.chat.send_message(message)
            
            print(f"Gemini raw response: {response}")

            # Check if Gemini wants to call a tool
            if response.candidates and response.candidates[0].content.parts:
                for i, part in enumerate(response.candidates[0].content.parts):
                    print(f"Part {i}: text={getattr(part, 'text', 'N/A')}, function_call={getattr(part, 'function_call', 'N/A')}")
                    if part.function_call:
                        function_call = part.function_call
                        print(f"Gemini wants to call tool: {function_call.name} with args {function_call.args}")
                        function_call.args = {key: value for key, value in function_call.args.items()}
                        print(function_call.args)
                        return function_call
                
                # If no function_call was found in any part, try to get text response
                try:
                    return self._split_message(response.text)
                except Exception as text_e:
                    print(f"Error converting response to text: {text_e}")
                    return [f"Error: Could not get a text response from Gemini. {text_e}"]
            else:
                print("No candidates or parts found in Gemini response.")
                return [f"Error: Could not get a response from Gemini. No candidates or parts."]
        except Exception as e:
            print(f"Error sending message to Gemini: {e}")
            return [f"Error: Could not get a response from Gemini. {e}"]
 
if __name__ == "__main__":
    # For example usage, you would need a mock DataFetcher or a real one
    class MockDataFetcher:
        def get_financial_data(self, categories: list[str], num_months: int) -> str:
            print(f"MockDataFetcher: get_financial_data called with categories={categories}, num_months={num_months}")
            return f"Mock financial data for {', '.join(categories)} for {num_months} months."
 
    mock_data_fetcher = MockDataFetcher()
    gemini_client = GeminiClient(mock_data_fetcher)
    
    # Example 1: Simple query
    print("--- Example 1: Simple Query ---")
    responses1 = gemini_client.send_message("What is the capital of France?")
    if isinstance(responses1, list):
        for i, res in enumerate(responses1):
            print(f"Gemini Part {i+1}: {res}")
    else:
        print(f"Gemini requested tool call: {responses1.name}({responses1.args})")
 
    # Example 2: Financial query that should trigger a tool call
    print("\n--- Example 2: Financial Query (Tool Call) ---")
    responses2 = gemini_client.send_message("How much did I spend on eating out in the last 3 months?")
    if isinstance(responses2, list):
        for i, res in enumerate(responses2):
            print(f"Gemini Part {i+1}: {res}")
    else:
        print(f"Gemini requested tool call: {responses2.name}({responses2.args})")
        # Simulate tool execution and sending response back
        tool_args_dict = {key: value for key, value in responses2.args.items()}
        tool_output = getattr(mock_data_fetcher, responses2.name)(**tool_args_dict)
        final_responses = gemini_client.send_message("How much did I spend on eating out in the last 3 months?", tool_output, responses2.name)
        if isinstance(final_responses, list):
            for i, res in enumerate(final_responses):
                print(f"Gemini Part {i+1}: {res}")
        else:
            print(f"Unexpected tool call after tool output: {final_responses.name}({final_responses.args})")
 
    # Example 3: Long prompt (simulated) - this part is removed as send_message now handles tool calls
    # print("\n--- Example 3: Long Prompt (Simulated) ---")
    # long_prompt = "a" * 12000 # This will exceed the max_gemini_input_length
    # responses3 = gemini_client.send_message(long_prompt)
    # for i, res in enumerate(responses3):
    #     print(f"Gemini Part {i+1}: {res}")