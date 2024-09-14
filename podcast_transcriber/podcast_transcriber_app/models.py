from django.db import models

# Create your models here.

class LibraryItem(models.Model):
    collection_id = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=255)
    artist = models.CharField(max_length=255)
    artwork_url = models.URLField()
    feed_url = models.URLField(blank=True, null=True)

    def __str__(self):
        return self.name

class Transcript(models.Model):
    podcast_name = models.CharField(max_length=255)
    episode_title = models.CharField(max_length=255)
    transcript_text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    publication_date = models.DateTimeField(null=True, blank=True)  # New field

    class Meta:
        unique_together = ('podcast_name', 'episode_title')

    def __str__(self):
        return f"{self.podcast_name} - {self.episode_title}"
