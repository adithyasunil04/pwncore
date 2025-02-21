from __future__ import annotations

import datetime
import typing as t
from logging import getLogger

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from passlib.hash import bcrypt
from pydantic import BaseModel
from tortoise.transactions import atomic

from pwncore.config import config
from pwncore.models import Team, User

# Metadata at the top for instant accessibility
metadata = {
    "name": "auth",
    "description": "Authentication using a JWT using a single access token.",
}

router = APIRouter(prefix="/auth", tags=["auth"])
logger = getLogger(__name__)


class AuthBody(BaseModel):
    name: str
    password: str


class SignupBody(BaseModel):
    name: str
    password: str
    tags: set[str]


def normalise_tag(tag: str):
    return tag.strip().casefold()


@atomic()
@router.post("/signup",
    description="""Create a new team with associated members.
    
    Request body example:
    ```json
    {
        "name": "TeamAwesome",
        "password": "securepassword123",
        "tags": ["user1", "user2", "user3"]
    }
    ```
    
    Responses:
    - 200: Successful signup
    ```json
    {
        "msg_code": 13
    }
    ```
    - 406: Team already exists
    ```json
    {
        "msg_code": 17
    }
    ```
    - 404: Users not found
    ```json
    {
        "msg_code": 24,
        "tags": ["user2", "user3"]
    }
    ```
    - 401: Users already in team
    ```json
    {
        "msg_code": 20,
        "tags": ["user1"]
    }
    ```
    - 500: Database error
    ```json
    {
        "msg_code": 0
    }
    ```
    """)
async def signup_team(team: SignupBody, response: Response):
    team.name = team.name.strip()
    members = set(map(normalise_tag, team.tags))

    try:
        if await Team.exists(name=team.name):
            response.status_code = 406
            return {"msg_code": config.msg_codes["team_exists"]}

        q = await User.filter(tag__in=members)
        # print(q, members)
        if len(q) != len(members):
            response.status_code = 404
            return {
                "msg_code": config.msg_codes["users_not_found"],
                "tags": list(members - set(map(lambda h: h.tag, q))),
            }
        in_teams = list(filter(lambda h: h.team, q))
        if in_teams:
            response.status_code = 401
            return {
                "msg_code": config.msg_codes["user_already_in_team"],
                "tags": list(in_teams),
            }

        newteam = await Team.create(
            name=team.name, secret_hash=bcrypt.hash(team.password)
        )

        for user in q:
            # Mypy kinda not working
            user.team_id = newteam.id  # type: ignore[attr-defined]
        if q:
            b = User.bulk_update(q, fields=["team_id"])
            # print(b.sql())
            await b
    except Exception:
        logger.exception("error in signup!")
        response.status_code = 500
        return {"msg_code": config.msg_codes["db_error"]}
    return {"msg_code": config.msg_codes["signup_success"]}


@router.post("/login",
    description="""Authenticate a team and receive a JWT token.
    
    Request body example:
    ```json
    {
        "name": "TeamAwesome",
        "password": "securepassword123"
    }
    ```
    
    Responses:
    - 200: Successful login
    ```json
    {
        "msg_code": 15,
        "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
        "token_type": "bearer"
    }
    ```
    - 404: Team not found
    ```json
    {
        "msg_code": 10
    }
    ```
    - 401: Wrong password
    ```json
    {
        "msg_code": 14
    }
    ```
    """)
async def team_login(team_data: AuthBody, response: Response):
    # TODO: Simplified logic since we're not doing refresh tokens.

    team = await Team.get_or_none(name=team_data.name)
    if team is None:
        response.status_code = 404
        return {"msg_code": config.msg_codes["team_not_found"]}
    if not bcrypt.verify(team_data.password, team.secret_hash):
        response.status_code = 401
        return {"msg_code": config.msg_codes["wrong_password"]}

    current_time = datetime.datetime.utcnow()
    expiration_time = current_time + datetime.timedelta(hours=config.jwt_valid_duration)
    token_payload = {"team_id": team.id, "exp": expiration_time}
    token = jwt.encode(token_payload, config.jwt_secret, algorithm="HS256")

    # Returning token to be sent as an authorization header "Bearer <TOKEN>"
    return {
        "msg_code": config.msg_codes["login_success"],
        "access_token": token,
        "token_type": "bearer",
    }


# Custom JWT processing (since FastAPI's implentation deals with refresh tokens)
# Supressing B008 in order to be able to use Header() in arguments
def get_jwt(*, authorization: t.Annotated[str, Header()]) -> JwtInfo:
    try:
        token = authorization.split(" ")[1]  # Remove Bearer
        # print(token, authorization)
        decoded_token: JwtInfo = jwt.decode(
            token, config.jwt_secret, algorithms=["HS256"]
        )
    except jwt.exceptions.InvalidTokenError as err:
        logger.warning("Invalid token", exc_info=err)
        raise HTTPException(status_code=401)
    return decoded_token


# Using a pre-assigned variable everywhere else to follow flake8's B008
JwtInfo = t.TypedDict("JwtInfo", {"team_id": int, "exp": int})
RequireJwt = t.Annotated[JwtInfo, Depends(get_jwt)]
