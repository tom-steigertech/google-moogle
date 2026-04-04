"""LLM Client for the Moogle bot - handles OpenAI interactions."""

import os
import logging
from openai import OpenAI


class MoogleLLMClient:
    """Encapsulates LLM interactions for the Moogle bot.
    
    This class handles all OpenAI API calls and can be easily tested
    in isolation from the Lambda handler.
    """
    
    DEFAULT_PERSONALITY = """You are a helpful and knowledgeable Moogle (モーグリ) from the Final Fantasy series! 

Your personality traits:
- You end many sentences with "kupo!" or "kupo kupo!"
- You are cheerful, friendly, and eager to help
- You have extensive knowledge of all Final Fantasy games, characters, lore, mechanics, and history
- You speak with a slightly whimsical but informative tone
- You sometimes reference Moogles' roles in various Final Fantasy games (like delivering mail, saving games, running shops, or being playable characters)
- You're particularly fond of mentioning that you have a pom-pom on your head

Answer questions about Final Fantasy games, characters, storylines, gameplay mechanics, and lore. Be thorough but keep responses concise (under 2000 characters for Slack).

Remember: Stay in character as a Moogle!"""

    def __init__(self, api_key: str = None, model: str = "gpt-4o-mini", 
                 log_level: str = "ERROR", system_prompt: str = None):
        """Initialize the LLM client.
        
        Args:
            api_key: OpenAI API key (defaults to OPENAI_API_KEY env var)
            model: OpenAI model to use (default: gpt-4o-mini)
            log_level: Logging level for this client
            system_prompt: Custom system prompt (uses default personality if not provided)
        """
        self.api_key = api_key or os.environ.get('OPENAI_API_KEY')
        if not self.api_key:
            raise ValueError("OpenAI API key is required. Provide it as argument or set OPENAI_API_KEY env var.")
        
        self.model = model
        self.client = OpenAI(api_key=self.api_key)
        self.system_prompt = system_prompt or self.DEFAULT_PERSONALITY
        
        # Setup logger
        self.logger = logging.getLogger(__name__)
        self._configure_logging(log_level)
    
    def _configure_logging(self, log_level: str):
        """Configure logging for this client."""
        valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        level = log_level.upper() if log_level.upper() in valid_levels else 'ERROR'
        self.logger.setLevel(getattr(logging, level))
        
        # Add handler if not already present
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            ))
            self.logger.addHandler(handler)
    
    def generate_response(self, question: str, max_tokens: int = 1000, 
                         temperature: float = 0.7) -> str:
        """Generate a Moogle-style response to the user's question.
        
        Args:
            question: The user's question
            max_tokens: Maximum tokens in response (default: 1000)
            temperature: Response creativity (default: 0.7)
            
        Returns:
            The generated response text
            
        Raises:
            Exception: If the OpenAI API call fails
        """
        if not question or not question.strip():
            self.logger.warning("Empty question received, using default")
            question = "Tell me about Final Fantasy!"
        
        self.logger.info(f"Calling OpenAI with model {self.model}")
        self.logger.debug(f"Question: {question[:100]}...")
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": question}
                ],
                max_tokens=max_tokens,
                temperature=temperature
            )
            
            result = response.choices[0].message.content
            self.logger.info(f"OpenAI response received, length: {len(result)}")
            self.logger.debug(f"Response preview: {result[:100]}...")
            
            return result
            
        except Exception as e:
            self.logger.error(f"OpenAI API error: {e}", exc_info=True)
            raise
    
    def set_personality(self, prompt: str):
        """Change the system prompt/personality.
        
        Useful for testing different personalities or scenarios.
        """
        self.system_prompt = prompt
        self.logger.info("System prompt updated")
    
    def set_model(self, model: str):
        """Change the model being used."""
        self.model = model
        self.logger.info(f"Model changed to: {model}")
    
    def get_personality(self) -> str:
        """Get the current system prompt."""
        return self.system_prompt
    
    def validate_configuration(self) -> bool:
        """Validate that the client is properly configured.
        
        Returns True if ready to make API calls, False otherwise.
        """
        if not self.api_key:
            self.logger.error("No API key configured")
            return False
        if not self.model:
            self.logger.error("No model configured")
            return False
        return True
