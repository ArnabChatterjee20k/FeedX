from ..database.models import URL, Hostname, CrawlState


class URLRow(URL):
    id: str
    sequence: int


class HostnameRow(Hostname):
    id: str
    sequence: int
