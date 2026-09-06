"""Recipe child editing services."""

from typing import Any, Dict, List

from django.db import transaction

from base.helpers.response import ServiceResponse
from stock.repositories import (
    RecipeRepository,
    RecipeStepRepository,
)


class RecipeStepService:
    @classmethod
    def serialize(cls, step) -> Dict[str, Any]:
        return {
            "id": step.id,
            "uuid": str(step.uuid),
            "recipe_id": step.recipe_id,
            "step_number": step.step_number,
            "title": step.title,
            "description": step.description,
            "duration_minutes": step.duration_minutes,
            "temperature": step.temperature,
            "equipment_needed": step.equipment_needed,
            "is_checkpoint": step.is_checkpoint,
            "photo_url": step.photo_url,
        }

    @classmethod
    @transaction.atomic
    def add(
        cls,
        recipe_id: int,
        step_number: int,
        title: str,
        description: str = "",
        duration_minutes: int = None,
        temperature: str = "",
        equipment_needed: str = "",
        is_checkpoint: bool = False,
        photo_url: str = "",
    ) -> Dict[str, Any]:
        recipe = RecipeRepository.get_by_id(recipe_id)
        if not recipe:
            return ServiceResponse.not_found("Recipe not found")

        if RecipeStepRepository.model.objects.filter(
            recipe=recipe, step_number=step_number
        ).exists():
            # Descending order avoids transient duplicate positions and every
            # save publishes the shifted step to the peer.
            shifted = (
                RecipeStepRepository.model.objects.select_for_update()
                .filter(recipe=recipe, step_number__gte=step_number)
                .order_by("-step_number")
            )
            for existing in shifted:
                existing.step_number += 1
                existing.save(update_fields=["step_number"])

        step = RecipeStepRepository.create(
            recipe=recipe,
            step_number=step_number,
            title=title,
            description=description,
            duration_minutes=duration_minutes,
            temperature=temperature,
            equipment_needed=equipment_needed,
            is_checkpoint=is_checkpoint,
            photo_url=photo_url,
        )

        return ServiceResponse.success(
            data={"id": step.id, "step": cls.serialize(step)}, message="Step added"
        )

    @classmethod
    @transaction.atomic
    def update(cls, step_id: int, **kwargs) -> Dict[str, Any]:
        step = RecipeStepRepository.get_by_id(step_id)
        if not step:
            return ServiceResponse.not_found("Step not found")

        for field in [
            "title",
            "description",
            "duration_minutes",
            "temperature",
            "equipment_needed",
            "is_checkpoint",
            "photo_url",
        ]:
            if field in kwargs:
                setattr(step, field, kwargs[field])

        step.save()

        return ServiceResponse.success(
            data={"step": cls.serialize(step)}, message="Step updated"
        )

    @classmethod
    @transaction.atomic
    def remove(cls, step_id: int) -> Dict[str, Any]:
        step = RecipeStepRepository.get_by_id(step_id)
        if not step:
            return ServiceResponse.not_found("Step not found")

        recipe_id = step.recipe_id
        step_number = step.step_number

        step.delete()

        shifted = (
            RecipeStepRepository.model.objects.select_for_update()
            .filter(recipe_id=recipe_id, step_number__gt=step_number)
            .order_by("step_number")
        )
        for existing in shifted:
            existing.step_number -= 1
            existing.save(update_fields=["step_number"])

        return ServiceResponse.success(message="Step removed")

    @classmethod
    @transaction.atomic
    def reorder(cls, recipe_id: int, step_ids: List[int]) -> Dict[str, Any]:
        for idx, step_id in enumerate(step_ids, 1):
            step = (
                RecipeStepRepository.model.objects.select_for_update()
                .filter(
                    id=step_id,
                    recipe_id=recipe_id,
                )
                .first()
            )
            if step and step.step_number != idx:
                step.step_number = idx
                step.save(update_fields=["step_number"])

        return ServiceResponse.success(
            data={"reordered": len(step_ids)}, message="Steps reordered"
        )
