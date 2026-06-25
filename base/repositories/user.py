from base.repositories.base import BaseSyncRepository
from base.models import User


class UserRepository(BaseSyncRepository):
    model = User

    @classmethod
    def get_by_email(cls, email):
        try:
            return cls.model.objects.get(email=email, is_deleted=False)
        except cls.model.DoesNotExist:
            return None

    @classmethod
    def get_by_role(cls, role):
        return cls.model.objects.filter(is_deleted=False, role=role)

    @classmethod
    def get_active(cls):
        return cls.model.objects.filter(
            is_deleted=False,
            status=User.UserStatus.ACTIVE,
        )

    @classmethod
    def get_cashiers(cls):
        return cls.model.objects.filter(
            is_deleted=False,
            role=User.RoleChoices.CASHIER,
            status=User.UserStatus.ACTIVE,
        )

    @classmethod
    def get_pos_staff(cls):
        # Everyone shown on the monoblock login picker: cashiers + managers +
        # kitchen (CHEF) staff. Managers sit at the same login tier but carry
        # elevated in-app access; CHEF logs in here and the FE routes them to /kds.
        return cls.model.objects.filter(
            is_deleted=False,
            role__in=(User.RoleChoices.CASHIER, User.RoleChoices.MANAGER,
                      User.RoleChoices.CHEF),
            status=User.UserStatus.ACTIVE,
        )

    @classmethod
    def get_admins(cls):
        return cls.model.objects.filter(
            is_deleted=False,
            role=User.RoleChoices.ADMIN,
            status=User.UserStatus.ACTIVE,
        )
