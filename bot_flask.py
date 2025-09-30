import logging
import os
import random
import threading
import time
import json
import urllib.parse

import requests
import telebot
from telebot import apihelper

# ADICIONADO: Importações necessárias para o Flask
from flask import Flask, request

# ================= 1. CONFIGURAÇÃO E LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Lendo as variáveis de ambiente que serão configuradas no Render
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME")
# ADICIONADO: URL do seu Web App. O Render vai fornecer essa URL.
# Colocamos um valor padrão, mas vamos configurar o webhook dinamicamente.
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_URL = f"{RENDER_URL}/{TOKEN}"

if not all([TOKEN, TMDB_API_KEY, BOT_USERNAME]):
    logger.critical("ERRO DE CONFIGURAÇÃO: Variáveis de ambiente não encontradas.")
    # Em um app web, é melhor retornar um erro do que usar exit()

# ================= 2. CLASSE DE CLIENTE DA API TMDB =================
class TMDBClient:
    """Encapsula todas as chamadas à API do The Movie Database."""
    BASE_URL = "https://api.themoviedb.org/3"
    IMAGE_BASE_URL = "https://image.tmdb.org/t/p/"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _make_request(self, endpoint: str, params: dict = None) -> dict | None:
        default_params = {"api_key": self.api_key, "language": "pt-BR"}
        full_params = {**default_params, **(params or {})}
        url = f"{self.BASE_URL}/{endpoint}"
        try:
            response = requests.get(url, params=full_params, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Erro na requisição à API TMDB para {url}: {e}")
            return None

    def get_poster_url(self, poster_path: str, size: str = "w500") -> str | None:
        return f"{self.IMAGE_BASE_URL}{size}{poster_path}" if poster_path else None

    def get_movies(self, category: str, page: int = 1) -> list:
        return (self._make_request(f"movie/{category}", {"page": page, "region": "BR"}) or {}).get("results", [])

    def get_classic_movies(self, page: int = 1) -> list:
        params = {
            "page": page, "sort_by": "popularity.desc", "vote_average.gte": 7.5,
            "vote_count.gte": 500, "primary_release_date.lte": "2000-12-31"
        }
        return (self._make_request("discover/movie", params) or {}).get("results", [])

    def get_details(self, media_type: str, content_id: int) -> dict | None:
        params = {"append_to_response": "credits,watch/providers,videos"}
        return self._make_request(f"{media_type}/{content_id}", params)

# ================= 3. CLASSE PRINCIPAL DO BOT =================
class CinemaBot:
    """Gerencia a lógica, estado e handlers do bot."""
    def __init__(self, token: str, tmdb_client: TMDBClient):
        self.bot = telebot.TeleBot(token, parse_mode='Markdown')
        self.tmdb = tmdb_client
        self.subscribed_chats = set()
        self.load_state()
        self.register_handlers()

    def load_state(self):
        try:
            # No Render, o sistema de arquivos pode ser temporário. 
            # Para persistência real, seria necessário um banco de dados, mas para este exemplo, o arquivo funcionará.
            with open("persistence.json", "r") as f:
                self.subscribed_chats = set(json.load(f).get("subscribed_chats", []))
                logger.info(f"Estado carregado: {len(self.subscribed_chats)} chats inscritos.")
        except (FileNotFoundError, json.JSONDecodeError):
            logger.warning("Arquivo de persistência não encontrado. Começando com estado vazio.")

    def save_state(self):
        with open("persistence.json", "w") as f:
            json.dump({"subscribed_chats": list(self.subscribed_chats)}, f)
        logger.info(f"Estado salvo.")

    @staticmethod
    def format_rating(rating: float) -> str:
        return "⭐" * int(round(rating or 0) / 2)

    def _format_movie_details_text(self, movie: dict) -> str:
        title = movie.get("title", "N/A")
        rating = movie.get("vote_average", 0.0)
        overview = movie.get("overview", "")
        release_date = movie.get("release_date", "N/A")

        text = f"🎬 *{title}*\n\n" \
               f"{self.format_rating(rating)} ({rating:.1f}/10)\n" \
               f"📅 *Lançamento:* {release_date}"
        
        if genres := [g['name'] for g in movie.get('genres', [])]:
            text += f"\n🎭 *Gêneros:* {', '.join(genres)}"
        if runtime_min := movie.get("runtime"):
            hours, minutes = divmod(runtime_min, 60)
            text += f"\n⏳ *Duração:* {hours}h {minutes}min"
        if overview:
            text += f"\n\n📖 *Sinopse:*\n_{overview}_"
        return text

    def register_handlers(self):
        self.bot.message_handler(commands=['start', 'ajuda'])(self.handle_start)
        self.bot.message_handler(commands=['stop'])(self.handle_stop)
        self.bot.message_handler(func=lambda msg: True)(self.handle_text_buttons)
        self.bot.callback_query_handler(func=lambda call: True)(self.handle_callback_query)

    def handle_start(self, message):
        chat_id = message.chat.id
        self.subscribed_chats.add(chat_id)
        self.save_state()
        
        markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.row('🎬 Em Cartaz', '🌟 Populares', '🚀 Em Breve')
        markup.row('🏆 Melhores Avaliados', '🏛️ Clássicos', '🎲 Sugestão')
        
        self.bot.send_message(
            chat_id,
            "Olá! Sou seu Bot de Cinema. Use os botões para descobrir filmes!",
            reply_markup=markup
        )

    def handle_stop(self, message):
        chat_id = message.chat.id
        if chat_id in self.subscribed_chats:
            self.subscribed_chats.remove(chat_id)
            self.save_state()
            self.bot.send_message(chat_id, "Você não receberá mais sugestões. Digite /start para reativar.")

    def handle_text_buttons(self, message):
        text = message.text
        actions = {
            '🎬 Em Cartaz': ('now_playing', '🎬 Filmes em Cartaz'),
            '🌟 Populares': ('popular', '🌟 Filmes Populares'),
            '🚀 Em Breve': ('upcoming', '🚀 Próximos Lançamentos'),
            '🏆 Melhores Avaliados': ('top_rated', '🏆 Filmes Mais Bem Avaliados'),
            '🏛️ Clássicos': ('classics', '🏛️ Clássicos do Cinema'),
        }
        if message.text == '🎲 Sugestão':
            self.send_suggestion(message.chat.id)
        elif action_params := actions.get(text):
            self.send_movie_list(chat_id=message.chat.id, category=action_params[0], title=action_params[1])
            
    def send_movie_list(self, chat_id: int, category: str, title: str):
        self.bot.send_message(chat_id, f"Buscando *{title}*...")
        
        movies = self.tmdb.get_classic_movies() if category == 'classics' else self.tmdb.get_movies(category)
        if not movies:
            self.bot.send_message(chat_id, "❌ Não encontrei filmes para esta categoria.")
            return

        markup = telebot.types.InlineKeyboardMarkup()
        for movie in movies[:5]:
            callback_data = f"details_{movie['id']}"
            markup.add(telebot.types.InlineKeyboardButton(
                text=f"{movie['title']} ({movie.get('release_date', '????')[:4]})",
                callback_data=callback_data
            ))
        
        self.bot.send_message(chat_id, f"Selecione um filme da lista de *{title}*:", reply_markup=markup)

    def send_suggestion(self, chat_id: int):
        self.bot.send_message(chat_id, "🎲 Procurando uma ótima sugestão...")
        movies = self.tmdb.get_movies('popular', page=random.randint(1, 10))
        if movies:
            self.show_movie_details(chat_id, random.choice(movies)['id'])
        else:
            self.bot.send_message(chat_id, "❌ Não consegui encontrar uma sugestão.")

    def handle_callback_query(self, call: telebot.types.CallbackQuery):
        self.bot.answer_callback_query(call.id)
        action, value = call.data.split('_', 1)
        if action == 'details':
            self.show_movie_details(call.message.chat.id, int(value), message_to_edit=call.message)

    def show_movie_details(self, chat_id: int, movie_id: int, message_to_edit: telebot.types.Message = None):
        movie = self.tmdb.get_details('movie', movie_id)
        if not movie:
            self.bot.send_message(chat_id, "❌ Erro ao obter os detalhes do filme.")
            return
            
        text = self._format_movie_details_text(movie)
        poster_url = self.tmdb.get_poster_url(movie.get('poster_path'))
        
        if message_to_edit:
            try:
                self.bot.delete_message(chat_id, message_to_edit.message_id)
            except Exception as e:
                logger.warning(f"Não foi possível deletar a mensagem: {e}")

        if poster_url:
            self.bot.send_photo(chat_id, poster_url, caption=text)
        else:
            self.bot.send_message(chat_id, text)

# ================= 4. LÓGICA DO FLASK (WEBHOOK) =================
# Cria a instância do bot e do cliente TMDB
tmdb_client = TMDBClient(api_key=TMDB_API_KEY)
cinema_bot_instance = CinemaBot(token=TOKEN, tmdb_client=tmdb_client)
bot = cinema_bot_instance.bot

# Cria a aplicação Flask
app = Flask(__name__)

# Rota principal, apenas para verificar se o app está no ar
@app.route('/')
def index():
    logger.info("Verificando se o bot está no ar...")
    bot.remove_webhook()
    time.sleep(0.1)
    # A variável de ambiente RENDER_EXTERNAL_URL é fornecida pelo Render
    bot.set_webhook(url=WEBHOOK_URL)
    logger.info("Webhook configurado!")
    return "Bot de Cinema está no ar e webhook foi reconfigurado!", 200

# Rota do Webhook: o Telegram vai enviar as atualizações para cá
@app.route('/' + TOKEN, methods=['POST'])
def get_message():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '!', 200
    else:
        telebot.abort(403)
