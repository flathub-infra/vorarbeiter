import procrastinate
from procrastinate import PsycopgConnector
from sqlalchemy.engine import make_url

from app.config import settings

conninfo = (
    make_url(settings.database_url)
    .set(drivername="postgresql")
    .render_as_string(hide_password=False)
)

app = procrastinate.App(
    connector=PsycopgConnector(conninfo=conninfo),
    import_paths=["app.tasks"],
)
