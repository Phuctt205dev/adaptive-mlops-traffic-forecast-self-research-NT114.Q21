import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.api.dependencies import require_admin
from backend.app.api.routes.users import router as users_router
from backend.app.core.errors import ApplicationError, register_exception_handlers
from backend.app.db.session import get_db
from backend.app.db.models import Base, User, UserRole
from backend.app.schemas.user import UserCreate, UserUpdate
from backend.app.services import users


class UserCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        Base.metadata.create_all(cls.engine)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(cls.engine)

    def setUp(self):
        self.session = Session(self.engine)
        self.session.query(User).delete()
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def make(self, email, role=UserRole.USER):
        return users.create_user(
            self.session, UserCreate(email=email, password="secret123"), role=role,
        )

    def active_admins(self):
        return self.session.scalar(
            select(func.count()).select_from(User).where(
                User.role == UserRole.ADMIN, User.is_active.is_(True),
            )
        )

    def refused(self, user, **changes):
        with self.assertRaises(ApplicationError) as caught:
            users.update_user(self.session, user.id, UserUpdate(**changes))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.code, "last_admin_required")


class AdminProtectionTests(UserCase):
    def test_last_active_admin_cannot_lose_the_role_or_be_disabled(self):
        only = self.make("only@example.com", UserRole.ADMIN)
        self.make("plain@example.com")
        self.refused(only, role="user")
        self.refused(only, is_active=False)
        self.refused(only, role="user", is_active=False)
        self.assertEqual(self.active_admins(), 1)
        self.assertEqual(users.get_user_or_404(self.session, only.id).role, UserRole.ADMIN)

    def test_a_disabled_admin_does_not_count_as_a_second_admin(self):
        first = self.make("first@example.com", UserRole.ADMIN)
        second = self.make("second@example.com", UserRole.ADMIN)
        users.update_user(self.session, second.id, UserUpdate(is_active=False))
        self.refused(first, role="user")

    def test_an_admin_can_step_down_when_another_active_admin_remains(self):
        first = self.make("first@example.com", UserRole.ADMIN)
        self.make("second@example.com", UserRole.ADMIN)
        users.update_user(self.session, first.id, UserUpdate(role="user"))
        self.assertEqual(self.active_admins(), 1)

    def test_other_changes_to_the_last_admin_are_still_allowed(self):
        only = self.make("only@example.com", UserRole.ADMIN)
        updated = users.update_user(self.session, only.id, UserUpdate(role="admin", is_active=True))
        self.assertEqual(updated.role, UserRole.ADMIN)
        plain = self.make("plain@example.com")
        users.update_user(self.session, plain.id, UserUpdate(role="admin"))
        users.update_user(self.session, plain.id, UserUpdate(is_active=False))
        self.assertEqual(self.active_admins(), 1)

    def test_null_values_do_not_wipe_columns(self):
        user = self.make("plain@example.com")
        updated = users.update_user(self.session, user.id, UserUpdate(role=None, is_active=None))
        self.assertEqual(updated.role, UserRole.USER)
        self.assertTrue(updated.is_active)

    def test_missing_user_is_still_reported(self):
        import uuid
        with self.assertRaises(ApplicationError) as caught:
            users.update_user(self.session, uuid.uuid4(), UserUpdate(role="user"))
        self.assertEqual(caught.exception.status_code, 404)


class AdminApiTests(UserCase):
    def client(self, acting_admin):
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(users_router, prefix="/api/v1/admin/users")
        app.dependency_overrides[get_db] = lambda: self.session
        app.dependency_overrides[require_admin] = lambda: acting_admin
        return TestClient(app)

    def test_api_returns_409_with_a_readable_reason_for_the_last_admin(self):
        only = self.make("only@example.com", UserRole.ADMIN)
        response = self.client(only).patch(f"/api/v1/admin/users/{only.id}", json={"is_active": False})
        self.assertEqual(response.status_code, 409)
        error = response.json()["error"]
        self.assertEqual(error["code"], "last_admin_required")
        self.assertIn("administrator", error["message"])
        self.assertEqual(self.active_admins(), 1)

    def test_api_allows_the_change_once_another_admin_exists(self):
        first = self.make("first@example.com", UserRole.ADMIN)
        self.make("second@example.com", UserRole.ADMIN)
        response = self.client(first).patch(f"/api/v1/admin/users/{first.id}", json={"role": "user"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["role"], "user")


class BootstrapPolicyTests(UserCase):
    def bootstrap(self):
        users.ensure_bootstrap_admin(self.session, "Boot@Example.com", "password123")

    def test_missing_account_is_created_as_admin(self):
        self.bootstrap()
        created = users.get_user_by_email(self.session, "boot@example.com")
        self.assertEqual(created.role, UserRole.ADMIN)
        self.assertTrue(created.is_active)

    def test_existing_admin_is_left_untouched(self):
        self.bootstrap()
        before = users.get_user_by_email(self.session, "boot@example.com")
        stamp = before.updated_at
        self.bootstrap()
        self.assertEqual(users.get_user_by_email(self.session, "boot@example.com").updated_at, stamp)

    def test_demoted_bootstrap_account_stays_demoted_while_another_admin_exists(self):
        self.bootstrap()
        boot = users.get_user_by_email(self.session, "boot@example.com")
        self.make("other@example.com", UserRole.ADMIN)
        users.update_user(self.session, boot.id, UserUpdate(role="user"))
        self.bootstrap()
        self.assertEqual(users.get_user_by_email(self.session, "boot@example.com").role, UserRole.USER)
        self.assertEqual(self.active_admins(), 1)

    def test_disabled_bootstrap_account_is_not_revived_while_another_admin_exists(self):
        self.bootstrap()
        boot = users.get_user_by_email(self.session, "boot@example.com")
        self.make("other@example.com", UserRole.ADMIN)
        users.update_user(self.session, boot.id, UserUpdate(is_active=False))
        self.bootstrap()
        self.assertFalse(users.get_user_by_email(self.session, "boot@example.com").is_active)

    def test_with_no_active_admin_the_bootstrap_account_is_restored(self):
        boot = self.make("boot@example.com")
        self.assertEqual(self.active_admins(), 0)
        self.bootstrap()
        restored = users.get_user_by_email(self.session, "boot@example.com")
        self.assertEqual(restored.role, UserRole.ADMIN)
        self.assertTrue(restored.is_active)
        self.assertEqual(restored.id, boot.id)

    def test_with_no_active_admin_a_disabled_bootstrap_admin_is_reactivated(self):
        boot = self.make("boot@example.com", UserRole.ADMIN)
        boot.is_active = False
        self.session.commit()
        self.bootstrap()
        self.assertTrue(users.get_user_by_email(self.session, "boot@example.com").is_active)

    def test_empty_settings_do_nothing(self):
        users.ensure_bootstrap_admin(self.session, "", "")
        self.assertEqual(self.session.scalar(select(func.count()).select_from(User)), 0)


if __name__ == "__main__":
    unittest.main()
