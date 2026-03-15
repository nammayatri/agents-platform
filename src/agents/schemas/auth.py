from pydantic import BaseModel


class RegisterInput(BaseModel):
    email: str
    display_name: str
    password: str


class LoginInput(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str
    role: str
    avatar_url: str | None = None
