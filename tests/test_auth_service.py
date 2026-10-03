import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.core.errors import ApplicationError
from backend.app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from backend.app.db.models import Base, User, UserRole
from backend.app.schemas.user import UserCreate, UserUpdate
from backend.app.services import users


class AuthServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
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

    def test_password_hash_and_verify(self):
        password_hash = hash_password("secret-password")

        self.assertTrue(verify_password("secret-password", password_hash))
        self.assertFalse(verify_password("wrong-password", password_hash))

    def test_create_authenticate_and_update_user(self):
        created = users.create_user(
            self.session,
            UserCreate(email="USER@Example.com", password="secret123"),
        )
        authenticated = users.authenticate_user(
            self.session,
            "user@example.com",
            "secret123",
        )
        updated = users.update_user(
            self.session,
            created.id,
            UserUpdate(role=UserRole.ADMIN),
        )

        self.assertEqual(created.email, "user@example.com")
        self.assertEqual(authenticated.id, created.id)
        self.assertIsNotNone(authenticated.last_login_at)
        self.assertEqual(updated.role, UserRole.ADMIN)

    def test_duplicate_email_is_rejected(self):
        payload = UserCreate(email="user@example.com", password="secret123")
        users.create_user(self.session, payload)

        with self.assertRaises(ApplicationError) as context:
            users.create_user(self.session, payload)

        self.assertEqual(context.exception.code, "email_conflict")
        self.assertEqual(context.exception.status_code, 409)

    def test_access_token_roundtrip(self):
        created = users.create_user(
            self.session,
            UserCreate(email="admin@example.com", password="secret123"),
            role=UserRole.ADMIN,
        )

        token = create_access_token(created.id, created.role.value)
        payload = decode_access_token(token)

        self.assertEqual(payload["sub"], str(created.id))
        self.assertEqual(payload["role"], "admin")


if __name__ == "__main__":
    unittest.main()
