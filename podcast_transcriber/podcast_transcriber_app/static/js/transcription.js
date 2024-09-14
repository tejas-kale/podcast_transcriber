let eventSource;

function startTranscription(audioUrl) {
    const csrftoken = getCookie('csrftoken');
    
    fetch('/start_transcription/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-CSRFToken': csrftoken
        },
        body: `audio_url=${encodeURIComponent(audioUrl)}`
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === "Transcription started") {
            console.log("Transcription started successfully");
            startSSE();
        } else {
            console.error("Failed to start transcription");
        }
    })
    .catch(error => console.error('Error:', error));
}

function startSSE() {
    eventSource = new EventSource("/sse/");
    
    eventSource.onmessage = function(event) {
        const data = JSON.parse(event.data);
        
        if (data.type === "transcription_text") {
            appendTranscription(data.text);
        } else if (data.type === "error") {
            console.error("Transcription error:", data.message);
            stopSSE();
        }
    };
    
    eventSource.onerror = function(error) {
        console.error("SSE error:", error);
        stopSSE();
    };
}

function appendTranscription(text) {
    const transcriptionDiv = document.getElementById('transcription');
    transcriptionDiv.innerHTML += text + ' ';
    transcriptionDiv.scrollTop = transcriptionDiv.scrollHeight;
}

function stopSSE() {
    if (eventSource) {
        eventSource.close();
        eventSource = null;
    }
}

function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== '') {
        const cookies = document.cookie.split(';');
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            if (cookie.substring(0, name.length + 1) === (name + '=')) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}

let debounceTimer;

function debounce(func, delay) {
    return function() {
        const context = this;
        const args = arguments;
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(() => func.apply(context, args), delay);
    };
}

function searchPodcasts() {
    const searchTerm = document.getElementById('search-input').value;
    console.log('Searching for:', searchTerm);
    
    // Show loading indicator
    const resultsContainer = document.getElementById('search-results');
    resultsContainer.innerHTML = '<p>Searching... This may take up to 5 minutes.</p>';
    resultsContainer.style.display = 'block';

    // Set a timeout for the fetch request (5 minutes)
    const timeoutDuration = 300000; // 5 minutes in milliseconds
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutDuration);

    fetch(`/search-podcasts/?q=${encodeURIComponent(searchTerm)}`, {
        signal: controller.signal
    })
        .then(response => {
            clearTimeout(timeoutId);
            console.log('Response status:', response.status);
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            return response.json();
        })
        .then(data => {
            console.log('Search results:', data);
            if (data.podcasts && Array.isArray(data.podcasts)) {
                displaySearchResults(data.podcasts);
            } else {
                console.error('Unexpected data structure:', data);
                displaySearchResults([]);
            }
        })
        .catch(error => {
            console.error('Error:', error);
            if (error.name === 'AbortError') {
                resultsContainer.innerHTML = '<p>Search request timed out after 5 minutes. Please try again.</p>';
            } else {
                resultsContainer.innerHTML = '<p>An error occurred while searching. Please try again.</p>';
            }
        });
}

function displaySearchResults(podcasts) {
    const resultsContainer = document.getElementById('search-results');
    resultsContainer.innerHTML = '';
    
    if (podcasts.length === 0) {
        resultsContainer.innerHTML = '<p>No results found.</p>';
        return;
    }

    podcasts.forEach(podcast => {
        const podcastElement = document.createElement('div');
        podcastElement.className = 'podcast-item';
        podcastElement.innerHTML = `
            <img src="${podcast.artworkUrl100}" alt="${podcast.collectionName}" class="podcast-artwork">
            <div class="podcast-info">
                <h3>${podcast.collectionName}</h3>
                <p>${podcast.artistName}</p>
                <button onclick="addToLibrary('${podcast.collectionId}', '${podcast.collectionName}', '${podcast.artistName}', '${podcast.artworkUrl100}')">Add to Library</button>
                <button onclick="showEpisodes('${podcast.collectionId}')">Show Episodes</button>
            </div>
        `;
        resultsContainer.appendChild(podcastElement);
    });
}

function addToLibrary(podcast) {
    fetch('{% url "add_to_library" %}', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-CSRFToken': getCookie('csrftoken')
        },
        body: new URLSearchParams({
            'collection_id': podcast.collectionId,
            'name': podcast.collectionName,
            'artist': podcast.artistName,
            'artwork_url': podcast.artworkUrl100
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === 'success') {
            alert('Podcast added to library!');
            updateLibraryList();
        } else if (data.status === 'already_exists') {
            alert('This podcast is already in your library.');
        } else {
            alert('Error adding podcast to library');
        }
    })
    .catch(error => {
        console.error('Error:', error);
        alert('Error adding podcast to library');
    });
}

function updateLibraryList() {
    fetch('{% url "get_library_items" %}')
        .then(response => response.json())
        .then(data => {
            const libraryList = document.getElementById('library-list');
            libraryList.innerHTML = '';
            data.library_items.forEach(item => {
                const div = document.createElement('div');
                div.className = 'library-item';
                div.setAttribute('data-podcast-id', item.collection_id);
                div.innerHTML = `
                    <img src="${item.artwork_url}" alt="${item.name}">
                    <div class="library-item-content">
                        <div>${item.name}</div>
                        <div class="episode-meta">${item.artist}</div>
                    </div>
                    <form method="post" action="{% url 'remove_from_library' item_id=0 %}".replace('0', item.collection_id)>
                        {% csrf_token %}
                        <button type="submit" class="remove-from-library" title="Remove from library">&times;</button>
                    </form>
                `;
                libraryList.appendChild(div);
            });
            addLibraryItemListeners();
        });
}

function showEpisodes(podcastId) {
    fetch(`/get_podcast_episodes/?podcast_id=${podcastId}`)
        .then(response => response.json())
        .then(data => {
            displayEpisodes(data.episodes);
        })
        .catch(error => console.error('Error:', error));
}

function displayEpisodes(episodes) {
    const resultsContainer = document.getElementById('search-results');
    resultsContainer.innerHTML = '<h2>Episodes</h2>';
    
    if (episodes.length === 0) {
        resultsContainer.innerHTML += '<p>No episodes found.</p>';
        return;
    }

    episodes.forEach(episode => {
        const episodeElement = document.createElement('div');
        episodeElement.className = 'episode-item';
        episodeElement.innerHTML = `
            <h3>${episode.trackName}</h3>
            <p>Duration: ${episode.duration_minutes} minutes</p>
            <p>Release Date: ${new Date(episode.releaseDate).toLocaleDateString()}</p>
            <button onclick="addToQueue('${episode.trackId}', '${episode.trackName}', '${episode.episodeUrl}', '${episode.collectionName}', '${episode.releaseDate}')">Add to Queue</button>
        `;
        resultsContainer.appendChild(episodeElement);
    });
}

function addToQueue(episodeId, episodeTitle, audioUrl, podcastName, publicationDate) {
    const csrftoken = getCookie('csrftoken');
    fetch('/add_to_queue/', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-CSRFToken': csrftoken
        },
        body: `episode_id=${episodeId}&episode_title=${encodeURIComponent(episodeTitle)}&audio_url=${encodeURIComponent(audioUrl)}&podcast_name=${encodeURIComponent(podcastName)}&publication_date=${encodeURIComponent(publicationDate)}`
    })
    .then(response => response.json())
    .then(data => {
        if (data.status === 'success') {
            alert('Added to transcription queue!');
        } else if (data.status === 'already_in_queue') {
            alert('This episode is already in the transcription queue.');
        }
    })
    .catch(error => console.error('Error:', error));
}

const debouncedSearch = debounce(searchPodcasts, 300);

document.getElementById('search-input').addEventListener('input', debouncedSearch);