import asyncio
import base64

from nano_alice.agent.tools.filesystem import ReadFileTool


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO5ZArsAAAAASUVORK5CYII="
)


def test_read_file_image_returns_metadata_instead_of_data_url(tmp_path) -> None:
    image_path = tmp_path / "pixel.png"
    image_path.write_bytes(PNG_1X1)

    tool = ReadFileTool(workspace=tmp_path)
    result = asyncio.run(tool.execute(str(image_path)))

    assert isinstance(result, list)
    assert result[0]["type"] == "image_file"
    assert result[0]["path"] == str(image_path)
    assert result[0]["mime_type"] == "image/png"
    assert result[0]["width"] == 1
    assert result[0]["height"] == 1
    assert "image_url" not in result[0]
    assert "data:image" not in result[1]["text"]
