import json
import logging
import os
import queue
import subprocess
import tempfile
import threading
import time
from urllib.parse import unquote, urlparse

import requests
from django.apps import AppConfig
from django.conf import settings
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models.signals import post_migrate
from django.dispatch import receiver
from django.http import StreamingHttpResponse, JsonResponse, HttpResponse
from django.shortcuts import render, redirect
from django.template.defaulttags import register
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt
from requests.exceptions import RequestException, Timeout

from .models import Transcript, LibraryItem

# Configure logging
log_file_path = os.path.join(settings.BASE_DIR, 'app.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(log_file_path),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Use a queue for SSE messages to handle concurrent requests
StreamingHttpResponse.sse_queue = queue.Queue()

# Whisper.cpp configuration
WHISPER_CPP_REPO = "https://github.com/ggerganov/whisper.cpp.git"
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
MODEL_PATH = os.path.join(settings.BASE_DIR, "models", "ggml-base.en.bin")
WHISPER_CPP_DIR = os.path.join(settings.BASE_DIR, "whisper.cpp")

def download_model():
    """
    Download the Whisper model if it doesn't exist.

    This function ensures that we always have the required model file
    before starting the transcription process.

    :raises: RequestException if there's an error downloading the model
    :return: None
    """
    if not os.path.exists(MODEL_PATH):
        logger.info("Downloading Whisper model...")
        os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
        response = requests.get(MODEL_URL)
        response.raise_for_status()
        with open(MODEL_PATH, "wb") as f:
            f.write(response.content)
        logger.info("Model downloaded successfully.")

def convert_audio(input_file, output_file):
    """
    Convert the input audio file to the format required by Whisper.cpp.

    We use ffmpeg for conversion because it's robust and can handle various input formats.
    The output is set to 16kHz, mono, 16-bit PCM as required by Whisper.cpp.

    :param input_file: str, path to the input audio file
    :param output_file: str, path to save the converted audio file
    :raises: subprocess.CalledProcessError if ffmpeg conversion fails
    :return: None
    """
    logger.info("Converting audio...")
    subprocess.run([
        "ffmpeg", "-y", "-i", input_file,
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        output_file
    ], check=True)
    logger.info("Audio converted successfully.")

def transcribe_audio(input_file, sse_url, podcast_name, episode_title, publication_date):
    """
    Transcribe the audio file using Whisper.cpp.

    This function handles the core transcription process, including checking for
    existing transcripts, running the Whisper.cpp binary, and saving the results.

    :param input_file: str, path to the input audio file
    :param sse_url: str, URL for sending Server-Sent Events
    :param podcast_name: str, name of the podcast
    :param episode_title: str, title of the episode
    :param publication_date: str, publication date of the episode
    :return: Transcript object or None if transcription fails
    """
    logger.info(f"Starting transcription for {podcast_name} - {episode_title}")
    main_script = os.path.join(WHISPER_CPP_DIR, "main")
    logger.info(f"Using main script: {main_script}")

    # Check for existing transcript to avoid unnecessary processing
    existing_transcript = Transcript.objects.filter(podcast_name=podcast_name, episode_title=episode_title).first()
    if existing_transcript and existing_transcript.transcript_text:
        logger.info(f"Non-empty transcript already exists for podcast: {podcast_name}, episode: {episode_title}")
        send_sse_message(sse_url, {"type": "existing_transcript", "text": existing_transcript.transcript_text})
        return existing_transcript

    # Run Whisper.cpp transcription process
    logger.info("Starting whisper.cpp transcription process")
    process = subprocess.Popen([
        main_script, "-m", MODEL_PATH,
        "-f", input_file,
        "--no-timestamps",
        "--no-prints",
        "--print-progress",
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, text=True)

    transcript_lines = []
    for line in process.stdout:
        line = line.strip()
        if line:
            logger.info(f"Transcription line: {line}")
            transcript_lines.append(line)
            send_sse_message(sse_url, {"type": "transcription_text", "text": line})

    process.wait()
    if process.returncode != 0:
        error_message = f"Transcription failed. Return code: {process.returncode}"
        logger.error(error_message)
        send_sse_message(sse_url, {"type": "error", "message": error_message})
        return None

    # Process and save the transcription result
    logger.info("Transcription process completed successfully")
    if transcript_lines and transcript_lines[-1].startswith("output_txt"):
        transcript_lines.pop()

    transcription = ' '.join(transcript_lines)
    logger.info(f"Transcription result (first 100 characters): {transcription[:100]}...")

    try:
        parsed_date = parse_datetime(publication_date) if publication_date else None
        transcript, created = Transcript.objects.update_or_create(
            podcast_name=podcast_name,
            episode_title=episode_title,
            defaults={
                'transcript_text': transcription,
                'publication_date': parsed_date
            }
        )
        logger.info(f"Transcript {'created' if created else 'updated'} for podcast: {podcast_name}, episode: {episode_title}")
        send_sse_message(sse_url, {"type": "transcription_complete", "text": transcription})
        return transcript
    except Exception as e:
        logger.error(f"Error saving transcript: {str(e)}", exc_info=True)
        send_sse_message(sse_url, {"type": "error", "message": f"Error saving transcript: {str(e)}"})
        return None

@csrf_exempt
def start_transcription(request):
    """
    Handle the request to start a transcription.

    This view function initiates the transcription process in a separate thread
    to avoid blocking the main thread and allow for concurrent transcriptions.

    :param request: HttpRequest object
    :return: JsonResponse with status of the transcription start
    :rtype: JsonResponse
    """
    if request.method == 'POST':
        audio_url = request.POST.get('audio_url')
        podcast_name = request.POST.get('podcast_name')
        episode_title = request.POST.get('episode_title')
        publication_date = request.POST.get('publication_date')
        episode_id = request.POST.get('episode_id')
        logger.info(f"Received transcription request for podcast: {podcast_name}, episode: {episode_title}, published: {publication_date}")
        logger.info(f"Audio URL: {audio_url}")
        logger.info(f"Episode ID: {episode_id}")
        
        sse_url = request.build_absolute_uri(f'/sse/{episode_id}/')
        logger.info(f"SSE URL: {sse_url}")
        
        try:
            # Start transcription in a separate thread to avoid blocking
            thread = threading.Thread(target=download_and_transcribe, args=(audio_url, sse_url, podcast_name, episode_title, publication_date))
            thread.start()
            
            logger.info("Transcription thread started")
            return JsonResponse({"status": "Transcription started"})
        except Exception as e:
            logger.error(f"Error starting transcription: {str(e)}", exc_info=True)
            return JsonResponse({"error": f"Error starting transcription: {str(e)}"}, status=500)
    
    logger.error("Invalid request method for start_transcription")
    return JsonResponse({"error": "Invalid request method"}, status=400)

def download_and_transcribe(audio_url, sse_url, podcast_name, episode_title, publication_date):
    """
    Download the audio file and initiate the transcription process.

    This function handles the entire process from downloading the audio file
    to initiating the transcription. It includes error handling and retries
    for robustness against network issues or server restrictions.

    :param audio_url: str, URL of the audio file to download
    :param sse_url: str, URL for sending Server-Sent Events
    :param podcast_name: str, name of the podcast
    :param episode_title: str, title of the episode
    :param publication_date: str, publication date of the episode
    :return: None
    """
    try:
        logger.info(f"Starting download_and_transcribe for {podcast_name} - {episode_title}")
        
        # Ensure Whisper.cpp is available
        logger.info("Checking whisper.cpp...")
        if not os.path.exists(os.path.join(WHISPER_CPP_DIR, "main")):
            logger.info("Cloning and building whisper.cpp...")
            subprocess.run(["git", "clone", WHISPER_CPP_REPO, WHISPER_CPP_DIR], check=True)
            os.chdir(WHISPER_CPP_DIR)
            subprocess.run(["make"], check=True)
            os.chdir(settings.BASE_DIR)
        
        logger.info("Downloading model if necessary...")
        download_model()

        logger.info(f"Downloading audio file from {audio_url}...")
        decoded_audio_url = unquote(audio_url)
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': urlparse(decoded_audio_url).scheme + '://' + urlparse(decoded_audio_url).netloc,
        }

        # Implement retry mechanism for robustness
        max_retries = 3
        retry_delay = 5  # seconds

        for attempt in range(max_retries):
            try:
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as temp_mp3:
                    response = requests.get(decoded_audio_url, stream=True, timeout=30, headers=headers)
                    response.raise_for_status()
                    for chunk in response.iter_content(chunk_size=8192):
                        temp_mp3.write(chunk)
                    input_file = temp_mp3.name
                logger.info(f"Audio file downloaded: {input_file}")
                break
            except RequestException as e:
                if attempt < max_retries - 1:
                    logger.warning(f"Download attempt {attempt + 1} failed: {str(e)}. Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    logger.error(f"Error downloading audio file after {max_retries} attempts: {str(e)}")
                    send_sse_message(sse_url, {"type": "error", "message": f"Error downloading audio file: {str(e)}"})
                    return

        logger.info("Converting audio to WAV...")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_wav:
            wav_file = temp_wav.name
        
        convert_audio(input_file, wav_file)
        logger.info(f"Audio converted to WAV: {wav_file}")
        
        logger.info("Starting transcription...")
        transcribe_audio(wav_file, sse_url, podcast_name, episode_title, publication_date)
        
        logger.info("Cleaning up temporary files...")
        os.unlink(input_file)
        os.unlink(wav_file)
        
        logger.info("Transcription process completed successfully")
        
    except Exception as e:
        logger.error(f"Error during transcription: {str(e)}", exc_info=True)
        send_sse_message(sse_url, {"type": "error", "message": f"Error during transcription: {str(e)}"})
    finally:
        logger.info(f"Finished download_and_transcribe for {podcast_name} - {episode_title}")
        send_sse_message(sse_url, {"type": "transcription_complete"})

def send_sse_message(sse_url, data):
    """
    Send a Server-Sent Event (SSE) message to the client.
    
    This function is used to update the client with the transcription progress
    and other relevant information.
    """
    try:
        json_data = json.dumps(data)
        response = requests.post(sse_url, data=json_data, headers={'Content-Type': 'application/json'})
        response.raise_for_status()
        logger.info(f"SSE message sent successfully: {json_data}")
    except requests.RequestException as e:
        logger.error(f"Error sending SSE message: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error sending SSE message: {str(e)}")

def search_itunes(query):
    """
    Search for podcasts on iTunes using the given query.
    
    This function is used to fetch podcast search results from iTunes.
    """
    url = f"https://itunes.apple.com/search?term={query}&entity=podcast&limit=10"
    logger.info(f"Sending request to iTunes API: {url}")
    try:
        response = requests.get(url, timeout=300)  # Set a 5-minute timeout
        response.raise_for_status()  # This will raise an exception for HTTP errors
        data = response.json()
        logger.info(f"Received {len(data.get('results', []))} results from iTunes API")
        return data.get('results', [])
    except Timeout:
        logger.error("Timeout error when fetching data from iTunes API")
        return []
    except RequestException as e:
        logger.error(f"Error fetching data from iTunes API: {str(e)}")
        return []
    except ValueError as e:
        logger.error(f"Error parsing JSON response from iTunes API: {str(e)}")
        return []

def get_podcast_episodes(podcast_id):
    """
    Get the episodes of a podcast from iTunes using the podcast ID.
    
    This function is used to fetch the episodes of a specific podcast from iTunes.
    """
    url = f"https://itunes.apple.com/lookup?id={podcast_id}&entity=podcastEpisode&limit=50"
    response = requests.get(url)
    if response.status_code == 200:
        data = response.json()
        return data.get('results', [])[1:]  # Skip the first result as it's the podcast info
    return []

@register.filter
def duration_in_minutes(milliseconds):
    """
    Convert the duration from milliseconds to minutes.
    
    This function is used to convert the duration of a podcast episode from milliseconds to minutes.
    """
    return int(milliseconds / 60000) if milliseconds else 0

def search_view(request):
    """
    Handle the search view for podcasts and episodes.
    
    This view function handles the search functionality for podcasts and episodes,
    including fetching search results, displaying podcast episodes, and managing
    the library and transcription queue.
    """
    search_result = request.GET.get('q', '')
    podcasts = []
    episodes = []
    podcast_name = ''
    page_obj = None

    if search_result:
        podcasts = search_itunes(search_result)

    podcast_id = request.GET.get('podcast_id')
    if podcast_id:
        episodes = get_podcast_episodes(podcast_id)
        if episodes:
            podcast_name = episodes[0]['collectionName']
        paginator = Paginator(episodes, 10)  # Show 10 episodes per page
        page_number = request.GET.get('page')
        page_obj = paginator.get_page(page_number)

    # Get library items from the database and sort them alphabetically
    library_items = LibraryItem.objects.all().order_by('name')

    # Get the transcription queue from the session and remove completed items
    transcription_queue = request.session.get('transcription_queue', [])
    transcription_queue = [item for item in transcription_queue if item['status'] != 'success']
    
    modified = False
    for item in transcription_queue:
        if item['status'] == 'in-progress':
            item['status'] = 'pending'
            modified = True
    
    if modified or len(transcription_queue) != len(request.session.get('transcription_queue', [])):
        request.session['transcription_queue'] = transcription_queue
        request.session.modified = True

    # Get latest episodes from all library podcasts
    latest_episodes = []
    for item in library_items:
        podcast_episodes = get_podcast_episodes(item.collection_id)
        for episode in podcast_episodes[:5]:
            episode['duration_minutes'] = duration_in_minutes(episode.get('trackTimeMillis', 0))
        latest_episodes.extend(podcast_episodes[:5])  # Get top 5 episodes from each podcast
    
    # Sort latest episodes by publication date
    latest_episodes.sort(key=lambda x: x['releaseDate'], reverse=True)
    
    # Paginate latest episodes
    latest_paginator = Paginator(latest_episodes, 10)
    latest_page_number = request.GET.get('latest_page')
    latest_page_obj = latest_paginator.get_page(latest_page_number)

    context = {
        'search_result': search_result,
        'podcasts': podcasts,
        'episodes': page_obj,
        'latest_episodes': latest_page_obj,
        'podcast_name': podcast_name,
        'library_items': library_items,
        'transcription_queue': transcription_queue,
    }
    return render(request, 'podcast_transcriber_app/search.html', context)

@csrf_exempt
def add_to_library(request):
    """
    Handle the request to add a podcast to the library.
    
    This view function handles the request to add a podcast to the user's library.
    """
    if request.method == 'POST':
        collection_id = request.POST.get('collection_id')
        name = request.POST.get('name')
        artist = request.POST.get('artist')
        artwork_url = request.POST.get('artwork_url')
        
        library_item, created = LibraryItem.objects.get_or_create(
            collection_id=collection_id,
            defaults={
                'name': name,
                'artist': artist,
                'artwork_url': artwork_url
            }
        )
        
        if created:
            return JsonResponse({"status": "success"})
        else:
            return JsonResponse({"status": "already_exists"})
    return JsonResponse({"status": "error"}, status=400)

def get_library_items(request):
    """
    Get the library items from the database.
    
    This function is used to retrieve the library items from the database.
    """
    library_items = LibraryItem.objects.all().order_by('name')
    return JsonResponse({"library_items": list(library_items.values())})

def remove_from_library(request, item_id):
    """
    Remove a podcast from the library.
    
    This view function handles the request to remove a podcast from the user's library.
    """
    if request.method == 'POST':
        try:
            library_item = LibraryItem.objects.get(collection_id=item_id)
            library_item.delete()
        except LibraryItem.DoesNotExist:
            pass
    return redirect('search')

@csrf_exempt
def sse_stream(request, episode_id):
    """
    Handle Server-Sent Events (SSE) for transcription updates.
    
    This view function handles the Server-Sent Events (SSE) for transcription updates,
    allowing the client to receive real-time updates on the transcription process.
    """
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            message = f"data: {json.dumps(data)}\n\n"
            StreamingHttpResponse.sse_queue.put(message)
            return HttpResponse(status=204)
        except json.JSONDecodeError:
            return HttpResponse("Invalid JSON", status=400)
    
    def event_stream():
        while True:
            try:
                message = StreamingHttpResponse.sse_queue.get(timeout=20)
                yield message
            except queue.Empty:
                yield 'data: {"type": "keepalive"}\n\n'
    
    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response

def set_media_permissions():
    """
    Set the appropriate permissions for the media directory.
    
    This function is used to set the appropriate permissions for the media directory
    to ensure that the server can read and write to it.
    """
    media_root = settings.MEDIA_ROOT
    os.makedirs(media_root, exist_ok=True)
    os.chmod(media_root, stat.S_IRWXU | stat.S_IRWXG | stat.S_IROTH | stat.S_IXOTH)
    for root, dirs, files in os.walk(media_root):
        for d in dirs:
            os.chmod(os.path.join(root, d), stat.S_IRWXU | stat.S_IRWXG | stat.S_IROTH | stat.S_IXOTH)
        for f in files:
            os.chmod(os.path.join(root, f), stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH)

def search_podcasts(request):
    """
    Handle the request to search for podcasts.
    
    This view function handles the request to search for podcasts using the iTunes API.
    """
    query = request.GET.get('q', '')
    logger.info(f"Received search query: {query}")
    
    if query:
        podcasts = search_itunes(query)
        logger.info(f"Found {len(podcasts)} podcasts")
        return JsonResponse({'podcasts': podcasts, 'debug_info': {'query': query, 'podcast_count': len(podcasts)}})
    
    logger.warning("No query provided for podcast search")
    return JsonResponse({'podcasts': [], 'debug_info': {'query': query, 'podcast_count': 0}})

@csrf_exempt
def add_to_queue(request):
    """
    Handle the request to add an episode to the transcription queue.
    
    This view function handles the request to add an episode to the transcription queue.
    """
    if request.method == 'POST':
        episode_id = request.POST.get('episode_id')
        episode_title = request.POST.get('episode_title')
        audio_url = request.POST.get('audio_url')
        podcast_name = request.POST.get('podcast_name')
        publication_date = request.POST.get('publication_date')
        
        queue = request.session.get('transcription_queue', [])
        if not any(item['episode_id'] == episode_id for item in queue):
            new_item = {
                'episode_id': episode_id,
                'episode_title': episode_title,
                'audio_url': audio_url,
                'podcast_name': podcast_name,
                'publication_date': publication_date,
                'status': 'pending'
            }
            queue.append(new_item)
            request.session['transcription_queue'] = queue
            request.session.modified = True
            return JsonResponse({'status': 'success'})
        else:
            return JsonResponse({'status': 'already_in_queue'})
    return JsonResponse({'status': 'error'}, status=400)

def remove_from_queue(request):
    """
    Handle the request to remove an episode from the transcription queue.
    
    This view function handles the request to remove an episode from the transcription queue.
    """
    if request.method == 'POST':
        episode_id = request.POST.get('episode_id')
        queue = request.session.get('transcription_queue', [])
        queue = [item for item in queue if item['episode_id'] != episode_id]
        request.session['transcription_queue'] = queue
        request.session.modified = True
        return JsonResponse({'status': 'success'})
    return JsonResponse({'status': 'error'}, status=400)

def get_queue(request):
    """
    Get the transcription queue from the session.
    
    This function is used to retrieve the transcription queue from the user's session.
    """
    queue = request.session.get('transcription_queue', [])
    return JsonResponse({'queue': queue})

def update_queue_status(request):
    """
    Update the status of an episode in the transcription queue.
    
    This view function handles the request to update the status of an episode in the transcription queue.
    """
    if request.method == 'POST':
        episode_id = request.POST.get('episode_id')
        new_status = request.POST.get('status')
        queue = request.session.get('transcription_queue', [])
        for item in queue:
            if item['episode_id'] == episode_id:
                item['status'] = new_status
                break
        request.session['transcription_queue'] = queue
        request.session.modified = True
        return JsonResponse({'status': 'success'})
    return JsonResponse({'status': 'error'}, status=400)

class MyAppConfig(AppConfig):
    """
    Configuration class for the Django app.
    
    This class is used to configure the Django app, including setting the default
    auto field and the app name.
    """
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'myapp'

    def ready(self):
        # This code runs when the app is loaded
        reset_transcription_queue()

@receiver(post_migrate)
def on_post_migrate(sender, **kwargs):
    # This ensures the queue is reset after migrations
    reset_transcription_queue()

def reset_transcription_queue():
    from django.contrib.sessions.models import Session
    from django.contrib.sessions.backends.db import SessionStore
    
    # Iterate through all sessions
    for session in Session.objects.all():
        session_data = session.get_decoded()
        if 'transcription_queue' in session_data:
            queue = session_data['transcription_queue']
            modified = False
            # Remove completed transcriptions and reset 'in-progress' to 'pending'
            queue = [item for item in queue if item['status'] != 'success']
            for item in queue:
                if item['status'] == 'in-progress':
                    item['status'] = 'pending'
                    modified = True
            if modified or len(queue) != len(session_data['transcription_queue']):
                session_store = SessionStore(session_key=session.session_key)
                session_store['transcription_queue'] = queue
                session_store.save()

import os
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from .models import Transcript
import logging

logger = logging.getLogger(__name__)

@csrf_exempt
def export_transcripts(request):
    """
    Export transcripts to text files.

    This function exports transcripts from the database to text files,
    one file per episode. The files are saved in a directory structure
    based on the podcast name and episode title.

    :param request: HttpRequest object
    :return: JsonResponse with the status and message of the export process
    :rtype: JsonResponse
    """
    if request.method == 'POST':
        base_path = '/Users/tejaskale/Library/Mobile Documents/com~apple~CloudDocs/Documents/Archive/Podcasts'
        
        try:
            transcripts = Transcript.objects.all()
            exported_count = 0
            skipped_count = 0
            failed_count = 0
            
            for transcript in transcripts:
                podcast_folder = os.path.join(base_path, transcript.podcast_name)
                os.makedirs(podcast_folder, exist_ok=True)
                
                file_name = f"{transcript.episode_title}.txt"
                file_path = os.path.join(podcast_folder, file_name)
                
                logger.info(f"Attempting to export: {transcript.podcast_name} - {transcript.episode_title}")
                
                if not os.path.exists(file_path):
                    if transcript.transcript_text:
                        try:
                            with open(file_path, 'w', encoding='utf-8') as f:
                                f.write(transcript.transcript_text)
                            exported_count += 1
                            logger.info(f"Successfully exported: {file_path}")
                        except Exception as e:
                            logger.error(f"Error exporting {file_path}: {str(e)}")
                            failed_count += 1
                    else:
                        logger.warning(f"Skipped empty transcript: {transcript.podcast_name} - {transcript.episode_title}")
                        failed_count += 1
                else:
                    logger.info(f"Skipped existing file: {file_path}")
                    skipped_count += 1
            
            logger.info(f"Export completed. Exported: {exported_count}, Skipped: {skipped_count}, Failed: {failed_count}")
            return JsonResponse({
                'status': 'success',
                'message': f'Exported {exported_count} new transcripts. Skipped {skipped_count} existing transcripts. Failed to export {failed_count} transcripts.'
            })
        except Exception as e:
            logger.error(f"Error during export: {str(e)}", exc_info=True)
            return JsonResponse({
                'status': 'error',
                'message': str(e)
            })
    
    return JsonResponse({
        'status': 'error',
        'message': 'Invalid request method'
    })

def get_podcast_episodes_view(request):
    """
    Get podcast episodes for a given podcast ID.

    This function retrieves the episodes of a podcast from iTunes
    based on the provided podcast ID. The episodes are paginated and
    sorted by release date in descending order.

    :param request: HttpRequest object
    :return: JsonResponse with the podcast episodes and pagination information
    :rtype: JsonResponse
    """
    podcast_id = request.GET.get('podcast_id')
    page = request.GET.get('page', 1)
    
    episodes = get_podcast_episodes(podcast_id)
    for episode in episodes:
        episode['duration_minutes'] = duration_in_minutes(episode.get('trackTimeMillis', 0))
    episodes.sort(key=lambda x: x['releaseDate'], reverse=True)
    
    paginator = Paginator(episodes, 10)
    
    try:
        episodes_page = paginator.page(page)
    except PageNotAnInteger:
        episodes_page = paginator.page(1)
    except EmptyPage:
        episodes_page = paginator.page(paginator.num_pages)
    
    return JsonResponse({
        'episodes': list(episodes_page),
        'current_page': episodes_page.number,
        'total_pages': paginator.num_pages,
        'has_next': episodes_page.has_next(),
        'has_previous': episodes_page.has_previous(),
        'next_page': episodes_page.next_page_number() if episodes_page.has_next() else None,
        'previous_page': episodes_page.previous_page_number() if episodes_page.has_previous() else None,
    })
