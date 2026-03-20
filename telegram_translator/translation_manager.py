import logging
from typing import Dict, Any, Optional
from abc import ABC, abstractmethod
import asyncio

logger = logging.getLogger(__name__)

class BaseTranslator(ABC):
    """Base class for translation providers"""
    
    @abstractmethod
    async def translate(self, text: str, source_lang: str = "auto", target_lang: str = "en") -> str:
        """Translate text from source language to target language"""
        pass

class IdiomaTranslator(BaseTranslator):
    """Idioma translation provider"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.source_lang = config.get('source_language', 'auto')
        self.target_lang = config.get('target_language', 'en')
        
        # Import idioma here to avoid import errors if not installed
        try:
            from idioma import Translator
            self.translator = Translator()
        except ImportError:
            raise ImportError("Idioma package is not installed. Run: pip install idioma")
    
    async def translate(self, text: str, source_lang: str = "auto", target_lang: str = "en") -> str:
        """Translate text using Idioma"""
        if not text:
            return ""
        
        try:
            # Use configured defaults if not specified
            source = source_lang if source_lang != "auto" else self.source_lang
            target = target_lang if target_lang != "en" else self.target_lang
            
            # Run translation in thread pool to avoid blocking
            result = await asyncio.to_thread(
                self.translator.translate,
                text,
                src=source,
                dest=target
            )
            
            return result.text if result and result.text else text
            
        except Exception as e:
            logger.error(f"Idioma translation error: {e}")
            return text

class OpenAITranslator(BaseTranslator):
    """OpenAI translation provider"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.api_key = config.get('api_key')
        self.model = config.get('model', 'gpt-4o-mini')
        self.max_tokens = config.get('max_tokens', 1000)
        self.temperature = config.get('temperature', 0.3)
        self.system_prompt = config.get('system_prompt', 
            "You are a professional translator. Translate the given text accurately while preserving meaning and tone. Respond with only the translated text.")
        
        if not self.api_key:
            raise ValueError("OpenAI API key is required for OpenAI translator")
        
        # Import openai here to avoid import errors if not installed
        try:
            import openai
            self.client = openai.AsyncOpenAI(api_key=self.api_key)
        except ImportError:
            raise ImportError("OpenAI package is not installed. Run: pip install openai")
    
    async def translate(self, text: str, source_lang: str = "auto", target_lang: str = "en") -> str:
        """Translate text using OpenAI"""
        if not text:
            return ""
        
        try:
            # Create user prompt with language context
            if source_lang != "auto":
                user_prompt = f"Translate the following text from {source_lang} to {target_lang}:\n\n{text}"
            else:
                user_prompt = f"Translate the following text to {target_lang}:\n\n{text}"
            
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=self.max_tokens,
                temperature=self.temperature
            )
            
            translated_text = response.choices[0].message.content.strip()
            
            # Clean up the response (remove quotes, extra formatting, etc.)
            if translated_text.startswith('"') and translated_text.endswith('"'):
                translated_text = translated_text[1:-1]
            
            return translated_text if translated_text else text
            
        except Exception as e:
            logger.error(f"OpenAI translation error: {e}")
            return text

class TranslationManager:
    """Manages translation providers and handles translation requests"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.provider = config.get('provider', 'openai')
        self.translator = self._create_translator()
    
    def _create_translator(self) -> BaseTranslator:
        """Create the appropriate translator based on configuration"""
        if self.provider == 'openai':
            openai_config = self.config.get('openai', {})
            return OpenAITranslator(openai_config)
        elif self.provider == 'idioma':
            idioma_config = self.config.get('idioma', {})
            return IdiomaTranslator(idioma_config)
        else:
            raise ValueError(f"Unsupported translation provider: {self.provider}")
    
    async def translate(self, text: str, source_lang: str = "auto", target_lang: str = "en") -> str:
        """Translate text using the configured provider"""
        if not text:
            return ""
        
        try:
            translated = await self.translator.translate(text, source_lang, target_lang)
            
            # Apply post-processing fixes
            translated = self._apply_post_processing(translated)
            
            return translated
            
        except Exception as e:
            logger.error(f"Translation failed: {e}")
            return text
    
    def _apply_post_processing(self, text: str) -> str:
        """Apply post-processing fixes to translated text"""
        # Fix common mistranslations
        fixes = {
            'Anxiety!': 'Alert!',  # "Тривога!" -> "Alert!"
            'Alarm!': 'Alert!',    # Alternative translation
        }
        
        for wrong, correct in fixes.items():
            text = text.replace(wrong, correct)
        
        return text
    
    def get_provider_info(self) -> Dict[str, Any]:
        """Get information about the current translation provider"""
        if self.provider == 'openai':
            return {
                'provider': 'openai',
                'model': self.config.get('openai', {}).get('model', 'unknown'),
                'max_tokens': self.config.get('openai', {}).get('max_tokens', 1000),
                'temperature': self.config.get('openai', {}).get('temperature', 0.3)
            }
        elif self.provider == 'idioma':
            return {
                'provider': 'idioma',
                'source_language': self.config.get('idioma', {}).get('source_language', 'auto'),
                'target_language': self.config.get('idioma', {}).get('target_language', 'en')
            }
        else:
            return {'provider': 'unknown'}
    
    def switch_provider(self, new_provider: str, new_config: Dict[str, Any] = None):
        """Switch to a different translation provider"""
        self.provider = new_provider
        if new_config:
            self.config.update(new_config)
        self.translator = self._create_translator()
        logger.info(f"Switched to translation provider: {new_provider}") 