# Clean Architecture for Python: A Practical Guide

**Version 1.0 | January 2026**

---

## Core Philosophy

Clean Architecture is about **dependency direction**. Every import must point **inward** toward the business logic core.

```
┌─────────────────────────────────────────────────┐
│              PRESENTATION (Routes, DTOs)        │
│  ┌─────────────────────────────────────────┐   │
│  │        INFRASTRUCTURE (DB, APIs)        │   │
│  │  ┌─────────────────────────────────┐   │   │
│  │  │      APPLICATION (Use Cases)    │   │   │
│  │  │  ┌─────────────────────────┐   │   │   │
│  │  │  │   DOMAIN (Entities)     │   │   │   │
│  │  │  └─────────────────────────┘   │   │   │
│  │  └─────────────────────────────────┘   │   │
│  └─────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
              Dependencies point INWARD →
```

**The Domain layer has ZERO external dependencies.** No FastAPI. No SQLAlchemy. No Pydantic. Just pure Python.

---

## The Four Layers

### Layer 1: Domain (The Core)
**Contains:** Entities, Value Objects, Domain Services, Business Rules  
**Imports:** Only Python standard library

```python
# ✅ ALLOWED              # ❌ FORBIDDEN
from dataclasses import   from fastapi import
from typing import        from sqlalchemy import
from enum import          from pydantic import
```

### Layer 2: Application (Use Cases)
**Contains:** Use Cases, Ports (interfaces), Application Services  
**Imports:** Domain layer + abstract interfaces

### Layer 3: Infrastructure (Adapters)
**Contains:** Repository implementations, API clients, File handlers  
**Imports:** Application interfaces + external libraries

### Layer 4: Presentation (Interface)
**Contains:** Routes, DTOs, Request/Response models, Error handlers  
**Imports:** All layers (outermost)

---

## Project Structure

```
src/
├── domain/
│   ├── entities/           # Objects with identity
│   │   └── user.py
│   ├── value_objects/      # Immutable concepts
│   │   └── email.py
│   ├── enums/
│   ├── services/           # Domain operations
│   └── exceptions.py
│
├── application/
│   ├── ports/              # Abstract interfaces
│   │   ├── repositories.py
│   │   └── gateways.py
│   ├── use_cases/
│   │   └── user/
│   │       ├── create_user.py
│   │       └── get_user.py
│   └── dto/
│
├── infrastructure/
│   ├── database/
│   │   ├── models.py       # ORM models
│   │   └── repositories/
│   │       └── sql_user_repository.py
│   ├── external/
│   │   └── sendgrid_gateway.py
│   └── config/
│
├── presentation/
│   └── api/
│       ├── routes/
│       ├── schemas/        # Pydantic models
│       └── dependencies.py
│
├── container.py            # DI container
└── main.py
```

---

## Domain Layer Implementation

### Entities

Entities have identity and encapsulate business rules:

```python
# domain/entities/user.py
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional
import uuid

from domain.value_objects.email import Email
from domain.enums.user_status import UserStatus
from domain.exceptions import DomainValidationError


@dataclass
class User:
    """
    User Entity with identity and lifecycle.
    
    Business Rules:
    - Username: 3-50 characters
    - Must be active to upgrade to premium
    """
    id: str
    username: str
    email: Email
    status: UserStatus = UserStatus.PENDING
    is_premium: bool = False
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: Optional[datetime] = None
    
    def __post_init__(self) -> None:
        self._validate()
    
    def _validate(self) -> None:
        errors: List[str] = []
        if not self.username or len(self.username) < 3:
            errors.append("Username must be at least 3 characters")
        if len(self.username) > 50:
            errors.append("Username cannot exceed 50 characters")
        if errors:
            raise DomainValidationError(errors)
    
    @classmethod
    def create(cls, username: str, email: str) -> "User":
        """Factory method for creating new users."""
        return cls(
            id=str(uuid.uuid4()),
            username=username,
            email=Email(email),
            status=UserStatus.PENDING
        )
    
    def activate(self) -> None:
        if self.status != UserStatus.PENDING:
            raise DomainValidationError([f"Cannot activate user with status {self.status}"])
        self.status = UserStatus.ACTIVE
        self.updated_at = datetime.utcnow()
    
    def upgrade_to_premium(self) -> None:
        if self.status != UserStatus.ACTIVE:
            raise DomainValidationError(["Only active users can upgrade"])
        self.is_premium = True
        self.updated_at = datetime.utcnow()
    
    def get_action_limit(self) -> int:
        return 1000 if self.is_premium else 100
```

### Value Objects

Immutable, compared by value, no identity:

```python
# domain/value_objects/email.py
from dataclasses import dataclass
import re
from domain.exceptions import DomainValidationError


@dataclass(frozen=True)  # Immutable
class Email:
    """Email Value Object - immutable, validated."""
    address: str
    
    def __post_init__(self) -> None:
        normalized = self.address.lower().strip()
        object.__setattr__(self, 'address', normalized)
        
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(pattern, normalized):
            raise DomainValidationError([f"Invalid email: {self.address}"])
    
    @property
    def domain(self) -> str:
        return self.address.split('@')[1]
    
    def __str__(self) -> str:
        return self.address
```

```python
# domain/value_objects/money.py
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from domain.exceptions import DomainValidationError


@dataclass(frozen=True)
class Money:
    """Money Value Object with currency awareness."""
    amount: Decimal
    currency: str
    
    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            object.__setattr__(self, 'amount', Decimal(str(self.amount)))
        object.__setattr__(self, 'currency', self.currency.upper())
        
        if self.amount < 0:
            raise DomainValidationError(["Amount cannot be negative"])
    
    def add(self, other: "Money") -> "Money":
        if self.currency != other.currency:
            raise DomainValidationError(["Currency mismatch"])
        return Money(self.amount + other.amount, self.currency)
    
    def multiply(self, factor: float) -> "Money":
        result = self.amount * Decimal(str(factor))
        return Money(result.quantize(Decimal('0.01'), ROUND_HALF_UP), self.currency)
```

### Domain Exceptions

```python
# domain/exceptions.py
from typing import List


class DomainException(Exception):
    """Base domain exception."""
    pass


class DomainValidationError(DomainException):
    def __init__(self, errors: List[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class EntityNotFoundError(DomainException):
    def __init__(self, entity_type: str, entity_id: str):
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(f"{entity_type} with ID {entity_id} not found")


class BusinessRuleViolationError(DomainException):
    def __init__(self, rule: str, context: str = ""):
        self.rule = rule
        super().__init__(f"Business rule violated: {rule}" + (f" ({context})" if context else ""))
```

---

## Application Layer Implementation

### Ports (Interfaces)

Define what the application needs from outside:

```python
# application/ports/repositories.py
from abc import ABC, abstractmethod
from typing import Optional, List
from domain.entities.user import User


class IUserRepository(ABC):
    """Port for User persistence."""
    
    @abstractmethod
    async def save(self, user: User) -> None: pass
    
    @abstractmethod
    async def find_by_id(self, user_id: str) -> Optional[User]: pass
    
    @abstractmethod
    async def find_by_email(self, email: str) -> Optional[User]: pass
    
    @abstractmethod
    async def exists_by_email(self, email: str) -> bool: pass
    
    @abstractmethod
    async def delete(self, user_id: str) -> bool: pass
```

```python
# application/ports/gateways.py
from abc import ABC, abstractmethod
from domain.entities.user import User


class IEmailGateway(ABC):
    """Port for email operations."""
    
    @abstractmethod
    async def send_welcome_email(self, user: User) -> bool: pass
    
    @abstractmethod
    async def send_activation_email(self, user: User, link: str) -> bool: pass
```

### Use Cases

Single-purpose application operations:

```python
# application/use_cases/user/create_user.py
from dataclasses import dataclass
from domain.entities.user import User
from domain.exceptions import BusinessRuleViolationError
from application.ports.repositories import IUserRepository
from application.ports.gateways import IEmailGateway


@dataclass
class CreateUserInput:
    username: str
    email: str
    send_welcome_email: bool = True


@dataclass
class CreateUserOutput:
    user_id: str
    username: str
    email: str
    status: str
    welcome_email_sent: bool


class CreateUserUseCase:
    """
    Use Case: Create a new user account.
    
    1. Check for duplicate email
    2. Create User entity
    3. Persist to repository
    4. Send welcome email (optional)
    """
    
    def __init__(self, user_repository: IUserRepository, email_gateway: IEmailGateway):
        self._user_repository = user_repository
        self._email_gateway = email_gateway
    
    async def execute(self, input_data: CreateUserInput) -> CreateUserOutput:
        # Check duplicate
        if await self._user_repository.exists_by_email(input_data.email):
            raise BusinessRuleViolationError("Email already registered", input_data.email)
        
        # Create entity (validates internally)
        user = User.create(username=input_data.username, email=input_data.email)
        
        # Persist
        await self._user_repository.save(user)
        
        # Optional email
        email_sent = False
        if input_data.send_welcome_email:
            email_sent = await self._email_gateway.send_welcome_email(user)
        
        return CreateUserOutput(
            user_id=user.id,
            username=user.username,
            email=str(user.email),
            status=user.status.value,
            welcome_email_sent=email_sent
        )
```

```python
# application/use_cases/user/get_user.py
from dataclasses import dataclass
from domain.exceptions import EntityNotFoundError
from application.ports.repositories import IUserRepository


@dataclass
class GetUserOutput:
    user_id: str
    username: str
    email: str
    status: str
    is_premium: bool


class GetUserUseCase:
    def __init__(self, user_repository: IUserRepository):
        self._user_repository = user_repository
    
    async def execute(self, user_id: str) -> GetUserOutput:
        user = await self._user_repository.find_by_id(user_id)
        if not user:
            raise EntityNotFoundError("User", user_id)
        
        return GetUserOutput(
            user_id=user.id,
            username=user.username,
            email=str(user.email),
            status=user.status.value,
            is_premium=user.is_premium
        )
```

---

## Infrastructure Layer Implementation

### Repository Implementation

```python
# infrastructure/database/repositories/sql_user_repository.py
from typing import Optional, List
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.entities.user import User
from domain.value_objects.email import Email
from domain.enums.user_status import UserStatus
from application.ports.repositories import IUserRepository
from infrastructure.database.models import UserModel


class SQLUserRepository(IUserRepository):
    """SQLAlchemy implementation - maps between domain and ORM."""
    
    def __init__(self, session: AsyncSession):
        self._session = session
    
    async def save(self, user: User) -> None:
        existing = await self._session.get(UserModel, user.id)
        if existing:
            existing.username = user.username
            existing.email = str(user.email)
            existing.status = user.status.value
            existing.is_premium = user.is_premium
            existing.updated_at = user.updated_at
        else:
            self._session.add(self._to_model(user))
        await self._session.commit()
    
    async def find_by_id(self, user_id: str) -> Optional[User]:
        model = await self._session.get(UserModel, user_id)
        return self._to_entity(model) if model else None
    
    async def find_by_email(self, email: str) -> Optional[User]:
        stmt = select(UserModel).where(UserModel.email == email.lower())
        result = await self._session.execute(stmt)
        model = result.scalar_one_or_none()
        return self._to_entity(model) if model else None
    
    async def exists_by_email(self, email: str) -> bool:
        stmt = select(UserModel.id).where(UserModel.email == email.lower())
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none() is not None
    
    async def delete(self, user_id: str) -> bool:
        model = await self._session.get(UserModel, user_id)
        if model:
            await self._session.delete(model)
            await self._session.commit()
            return True
        return False
    
    def _to_model(self, entity: User) -> UserModel:
        return UserModel(
            id=entity.id, username=entity.username, email=str(entity.email),
            status=entity.status.value, is_premium=entity.is_premium,
            created_at=entity.created_at, updated_at=entity.updated_at
        )
    
    def _to_entity(self, model: UserModel) -> User:
        return User(
            id=model.id, username=model.username, email=Email(model.email),
            status=UserStatus(model.status), is_premium=model.is_premium,
            created_at=model.created_at, updated_at=model.updated_at
        )
```

### ORM Models

```python
# infrastructure/database/models.py
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Boolean, DateTime
from sqlalchemy.orm import Mapped, mapped_column, DeclarativeBase


class Base(DeclarativeBase):
    pass


class UserModel(Base):
    __tablename__ = "users"
    
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(50), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
```

### Gateway Implementation

```python
# infrastructure/external/sendgrid_gateway.py
import httpx
from domain.entities.user import User
from application.ports.gateways import IEmailGateway


class SendGridEmailGateway(IEmailGateway):
    def __init__(self, api_key: str, from_email: str):
        self._api_key = api_key
        self._from_email = from_email
    
    async def send_welcome_email(self, user: User) -> bool:
        return await self._send(
            to=str(user.email),
            subject="Welcome!",
            html=f"<h1>Welcome, {user.username}!</h1>"
        )
    
    async def send_activation_email(self, user: User, link: str) -> bool:
        return await self._send(
            to=str(user.email),
            subject="Activate Your Account",
            html=f'<a href="{link}">Activate</a>'
        )
    
    async def _send(self, to: str, subject: str, html: str) -> bool:
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "personalizations": [{"to": [{"email": to}]}],
                        "from": {"email": self._from_email},
                        "subject": subject,
                        "content": [{"type": "text/html", "value": html}]
                    }
                )
                return response.status_code == 202
            except Exception:
                return False
```

### In-Memory Repository (Testing)

```python
# infrastructure/database/repositories/in_memory_user_repository.py
from typing import Optional, Dict
from domain.entities.user import User
from application.ports.repositories import IUserRepository


class InMemoryUserRepository(IUserRepository):
    """In-memory implementation for unit tests."""
    
    def __init__(self):
        self._users: Dict[str, User] = {}
    
    async def save(self, user: User) -> None:
        self._users[user.id] = user
    
    async def find_by_id(self, user_id: str) -> Optional[User]:
        return self._users.get(user_id)
    
    async def find_by_email(self, email: str) -> Optional[User]:
        for user in self._users.values():
            if str(user.email) == email.lower():
                return user
        return None
    
    async def exists_by_email(self, email: str) -> bool:
        return await self.find_by_email(email) is not None
    
    async def delete(self, user_id: str) -> bool:
        if user_id in self._users:
            del self._users[user_id]
            return True
        return False
    
    def clear(self) -> None:
        self._users.clear()
```

---

## Presentation Layer Implementation

### Pydantic Schemas

```python
# presentation/api/schemas/user_schemas.py
from pydantic import BaseModel, Field, EmailStr, field_validator


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    send_welcome_email: bool = True
    
    @field_validator('username')
    @classmethod
    def validate_username(cls, v: str) -> str:
        if not v.replace('_', '').replace('-', '').isalnum():
            raise ValueError('Invalid characters in username')
        return v.strip()


class UserResponse(BaseModel):
    user_id: str
    username: str
    email: str
    status: str
    is_premium: bool


class ErrorResponse(BaseModel):
    success: bool = False
    error_code: str
    message: str
    details: list[str] | None = None
```

### FastAPI Routes

```python
# presentation/api/routes/user_routes.py
from fastapi import APIRouter, Depends, HTTPException, status

from domain.exceptions import DomainValidationError, EntityNotFoundError, BusinessRuleViolationError
from application.use_cases.user.create_user import CreateUserUseCase, CreateUserInput
from application.use_cases.user.get_user import GetUserUseCase
from presentation.api.schemas.user_schemas import CreateUserRequest, UserResponse
from presentation.api.dependencies import get_create_user_use_case, get_get_user_use_case


router = APIRouter(prefix="/users", tags=["Users"])


@router.post("/", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    request: CreateUserRequest,
    use_case: CreateUserUseCase = Depends(get_create_user_use_case)
):
    try:
        result = await use_case.execute(CreateUserInput(
            username=request.username,
            email=request.email,
            send_welcome_email=request.send_welcome_email
        ))
        return UserResponse(
            user_id=result.user_id, username=result.username,
            email=result.email, status=result.status, is_premium=False
        )
    except DomainValidationError as e:
        raise HTTPException(400, {"error_code": "VALIDATION_ERROR", "message": str(e)})
    except BusinessRuleViolationError as e:
        raise HTTPException(409, {"error_code": "CONFLICT", "message": str(e)})


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str,
    use_case: GetUserUseCase = Depends(get_get_user_use_case)
):
    try:
        result = await use_case.execute(user_id)
        return UserResponse(
            user_id=result.user_id, username=result.username,
            email=result.email, status=result.status, is_premium=result.is_premium
        )
    except EntityNotFoundError as e:
        raise HTTPException(404, {"error_code": "NOT_FOUND", "message": str(e)})
```

---

## Dependency Injection

### Container

```python
# container.py
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from application.ports.repositories import IUserRepository
from application.ports.gateways import IEmailGateway
from application.use_cases.user.create_user import CreateUserUseCase
from application.use_cases.user.get_user import GetUserUseCase
from infrastructure.database.repositories.sql_user_repository import SQLUserRepository
from infrastructure.external.sendgrid_gateway import SendGridEmailGateway


class Container:
    def __init__(self, database_url: str, sendgrid_key: str, from_email: str):
        self._engine = create_async_engine(database_url)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._sendgrid_key = sendgrid_key
        self._from_email = from_email
    
    async def get_session(self) -> AsyncSession:
        return self._session_factory()
    
    def get_user_repository(self, session: AsyncSession) -> IUserRepository:
        return SQLUserRepository(session)
    
    def get_email_gateway(self) -> IEmailGateway:
        return SendGridEmailGateway(self._sendgrid_key, self._from_email)
    
    def get_create_user_use_case(self, repo: IUserRepository, gateway: IEmailGateway):
        return CreateUserUseCase(repo, gateway)
    
    def get_get_user_use_case(self, repo: IUserRepository):
        return GetUserUseCase(repo)
```

### FastAPI Dependencies

```python
# presentation/api/dependencies.py
from typing import AsyncGenerator
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from container import Container, get_container


async def get_db_session(container: Container = Depends(get_container)) -> AsyncGenerator:
    session = await container.get_session()
    try:
        yield session
    finally:
        await session.close()


def get_create_user_use_case(
    session: AsyncSession = Depends(get_db_session),
    container: Container = Depends(get_container)
):
    repo = container.get_user_repository(session)
    gateway = container.get_email_gateway()
    return container.get_create_user_use_case(repo, gateway)


def get_get_user_use_case(
    session: AsyncSession = Depends(get_db_session),
    container: Container = Depends(get_container)
):
    return container.get_get_user_use_case(container.get_user_repository(session))
```

---

## Testing Strategy

### Domain Tests (No Mocks)

```python
# tests/unit/domain/test_user_entity.py
import pytest
from domain.entities.user import User
from domain.enums.user_status import UserStatus
from domain.exceptions import DomainValidationError


class TestUserEntity:
    def test_create_user_valid(self):
        user = User.create(username="john_doe", email="john@example.com")
        assert user.status == UserStatus.PENDING
        assert user.is_premium is False
    
    def test_short_username_fails(self):
        with pytest.raises(DomainValidationError):
            User.create(username="ab", email="test@example.com")
    
    def test_activate_pending_user(self):
        user = User.create(username="john_doe", email="john@example.com")
        user.activate()
        assert user.status == UserStatus.ACTIVE
    
    def test_activate_active_user_fails(self):
        user = User.create(username="john_doe", email="john@example.com")
        user.activate()
        with pytest.raises(DomainValidationError):
            user.activate()
    
    def test_premium_limit(self):
        user = User.create(username="john_doe", email="john@example.com")
        user.activate()
        assert user.get_action_limit() == 100
        user.upgrade_to_premium()
        assert user.get_action_limit() == 1000
```

### Application Tests (With Mocks)

```python
# tests/unit/application/test_create_user.py
import pytest
from unittest.mock import AsyncMock
from application.use_cases.user.create_user import CreateUserUseCase, CreateUserInput
from domain.exceptions import BusinessRuleViolationError


class TestCreateUserUseCase:
    @pytest.fixture
    def mock_repo(self):
        mock = AsyncMock()
        mock.exists_by_email.return_value = False
        return mock
    
    @pytest.fixture
    def mock_gateway(self):
        mock = AsyncMock()
        mock.send_welcome_email.return_value = True
        return mock
    
    @pytest.mark.asyncio
    async def test_create_user_success(self, mock_repo, mock_gateway):
        use_case = CreateUserUseCase(mock_repo, mock_gateway)
        result = await use_case.execute(CreateUserInput("john", "john@test.com"))
        
        assert result.username == "john"
        mock_repo.save.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_duplicate_email_fails(self, mock_repo, mock_gateway):
        mock_repo.exists_by_email.return_value = True
        use_case = CreateUserUseCase(mock_repo, mock_gateway)
        
        with pytest.raises(BusinessRuleViolationError):
            await use_case.execute(CreateUserInput("john", "existing@test.com"))
```

---

## Common Patterns

### Result Pattern

```python
from dataclasses import dataclass
from typing import Generic, TypeVar, Optional, List

T = TypeVar('T')


@dataclass
class Result(Generic[T]):
    success: bool
    value: Optional[T] = None
    errors: Optional[List[str]] = None
    
    @classmethod
    def ok(cls, value: T) -> "Result[T]":
        return cls(success=True, value=value)
    
    @classmethod
    def fail(cls, errors: List[str]) -> "Result[T]":
        return cls(success=False, errors=errors)
```

### Domain Events

```python
from dataclasses import dataclass, field
from datetime import datetime
import uuid


@dataclass
class DomainEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class UserCreatedEvent(DomainEvent):
    user_id: str = ""
    email: str = ""
```

---

## Anti-Patterns

### ❌ Domain Imports Framework

```python
# BAD
from pydantic import BaseModel
class User(BaseModel): ...

# GOOD
from dataclasses import dataclass
@dataclass
class User: ...
```

### ❌ Business Logic in Routes

```python
# BAD
@router.post("/users")
async def create_user(request, db):
    if await db.query(User).filter(...).first():  # Business logic here!
        raise HTTPException(400, "Email exists")

# GOOD
@router.post("/users")
async def create_user(request, use_case = Depends(get_use_case)):
    result = await use_case.execute(...)  # Delegate to use case
```

### ❌ Repository Returns ORM Models

```python
# BAD
async def find_by_id(self, id: str) -> UserModel: ...

# GOOD
async def find_by_id(self, id: str) -> Optional[User]: ...  # Domain entity
```

### ❌ Concrete Dependencies

```python
# BAD
class CreateUserUseCase:
    def __init__(self):
        self._repo = SQLUserRepository()  # Concrete!

# GOOD
class CreateUserUseCase:
    def __init__(self, user_repository: IUserRepository):  # Interface!
        self._repo = user_repository
```

---

## Quick Reference

### Layer Rules

| Layer | Can Import | Cannot Import |
|-------|------------|---------------|
| Domain | Standard library | Any framework |
| Application | Domain, ABCs | Infrastructure, Presentation |
| Infrastructure | Domain, Application interfaces | Presentation |
| Presentation | All | - |

### Naming Conventions

| Type | Pattern | Example |
|------|---------|---------|
| Entity | `{name}.py` | `user.py` |
| Value Object | `{name}.py` | `email.py` |
| Use Case | `{verb}_{entity}.py` | `create_user.py` |
| Repository | `{db}_{entity}_repository.py` | `sql_user_repository.py` |
| Gateway | `{service}_gateway.py` | `stripe_gateway.py` |

### Dependencies

```
# Domain: NONE

# Infrastructure
sqlalchemy[asyncio]==2.0+
httpx==0.26+

# Presentation
fastapi==0.109+
pydantic==2.5+

# Testing
pytest==7.4+
pytest-asyncio==0.23+
```

---

## Summary

1. **Keep Domain Pure**: No framework imports
2. **Define Clear Interfaces**: Ports in Application, Adapters in Infrastructure
3. **Inject Dependencies**: Use cases receive dependencies, never create them
4. **Map at Boundaries**: DTOs ↔ Entities ↔ ORM Models
5. **Test in Layers**: Domain without mocks, Use cases with mocks

---

*Python 3.10+ | FastAPI 0.109+ | SQLAlchemy 2.0+*
