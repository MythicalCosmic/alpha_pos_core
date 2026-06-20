"""Persisted AI-assistant conversations (chat history).

The AI assistant used to be stateless — every question was a one-shot call with no
memory. These two models give it real chat history: a conversation (`AIChat`) holds
an ordered list of `AIMessage` turns, so the assistant can answer follow-ups in
context and the operator can reopen old chats.

Plain (non-synced) models: chat history is local to the back-office where the AI
runs, per signed-in operator, and has no cross-branch meaning. `user_id` is stored
as a raw id (whoever the request was authenticated as) rather than a hard FK, so it
works regardless of which auth user model the admin page runs under.
"""
from django.db import models


class AIChat(models.Model):
    user_id = models.IntegerField(null=True, blank=True, db_index=True)
    title = models.CharField(max_length=140, blank=True, default='')
    is_deleted = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        db_table = 'ai_chat'
        ordering = ['-updated_at']

    def __str__(self):
        return self.title or f'Chat #{self.pk}'


class AIMessage(models.Model):
    class Role(models.TextChoices):
        USER = 'user', 'User'
        ASSISTANT = 'assistant', 'Assistant'

    chat = models.ForeignKey(
        AIChat, on_delete=models.CASCADE, related_name='messages', db_index=True,
    )
    role = models.CharField(max_length=10, choices=Role.choices)
    content = models.TextField(blank=True, default='')
    # The assistant turn carried an error (no key / provider failure) rather than a
    # real answer — kept in history so the chat reflects what happened, but the
    # chat service skips replaying error turns back to the model as context.
    is_error = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ai_message'
        ordering = ['created_at', 'id']

    def __str__(self):
        return f'{self.role}: {self.content[:40]}'
