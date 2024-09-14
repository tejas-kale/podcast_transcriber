from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('podcast_transcriber_app.urls')),  # This should include your app's URLs
]