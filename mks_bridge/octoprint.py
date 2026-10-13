"""Совместимость с OctoPrint: Orca (хост-тип Octo/Klipper) проверяет сервер и присылает файл сюда."""
from email.parser import BytesParser
from email.policy import HTTP as HTTP_POLICY


class OctoPrintApi:
    def _octo_version(self):
        self._json(200, {"api": "0.1", "server": "1.9.0", "text": "OctoPrint 1.9.0 (MKS bridge)"})

    def _octo_printer(self):
        self._json(200, {"state": {"text": "Operational", "flags": {"operational": True}}})

    def _octo_files(self):
        self._json(200, {"files": []})

    def _orca_upload(self):
        if not self.headers.get("X-Api-Key"):
            self._json(403, {"error": "В Orca укажи любой непустой API-ключ."})
            return
        body = self._body()
        msg = BytesParser(policy=HTTP_POLICY).parsebytes(
            b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
        fname, data, start = None, None, False
        for part in msg.iter_parts():
            pname = part.get_param("name", header="content-disposition")
            if pname == "file":
                fname, data = part.get_filename(), part.get_payload(decode=True)
            elif pname == "print":
                start = part.get_content().strip().lower() == "true"
        if not fname or data is None:
            self._json(400, {"error": "no file in request"})
            return
        self._start_job(fname, data, start)
        self._json(201, {"files": {"local": {"name": fname, "origin": "local"}}, "done": True})
