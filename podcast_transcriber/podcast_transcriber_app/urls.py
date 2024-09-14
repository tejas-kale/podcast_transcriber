from django.urls import path
from . import views

urlpatterns = [
    path('', views.search_view, name='search'),
    path('add_to_library/', views.add_to_library, name='add_to_library'),
    path('remove_from_library/<str:item_id>/', views.remove_from_library, name='remove_from_library'),
    path('start_transcription/', views.start_transcription, name='start_transcription'),
    path('sse/<str:episode_id>/', views.sse_stream, name='sse_stream'),
    path('search-podcasts/', views.search_podcasts, name='search_podcasts'),
    path('get_library_items/', views.get_library_items, name='get_library_items'),
    path('add_to_queue/', views.add_to_queue, name='add_to_queue'),
    path('remove_from_queue/', views.remove_from_queue, name='remove_from_queue'),
    path('get_queue/', views.get_queue, name='get_queue'),
    path('update_queue_status/', views.update_queue_status, name='update_queue_status'),
    path('export_transcripts/', views.export_transcripts, name='export_transcripts'),
    path('get_podcast_episodes/', views.get_podcast_episodes_view, name='get_podcast_episodes'),
]