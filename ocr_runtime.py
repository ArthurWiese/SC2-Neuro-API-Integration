from __future__ import annotations

import asyncio
from collections import deque
from difflib import SequenceMatcher
import re
import time
import uuid
from pathlib import Path

from PIL import Image, ImageChops, ImageEnhance, ImageGrab, ImageOps, ImageStat
import pytesseract

OCR_REGION_COUNT = 2
OCR_WHITE_PIXEL_THRESHOLD = 200
OCR_BORDER_SEARCH_DISTANCE = 5
OCR_DIALOGUE_BORDER_COLOR_DELTA = 15
OCR_DIALOGUE_BORDER_MAX_CHANNEL = 140
OCR_DIALOGUE_BORDER_MAX_CHANNEL_DELTA = 24
OCR_DIALOGUE_BORDER_MAX_SEGMENT_GAP = 100
OCR_DIALOGUE_BORDER_PADDING = 3
OCR_TEXT_CROP_BUFFER = 12
OCR_TEXT_CROP_LEFT_BUFFER = OCR_TEXT_CROP_BUFFER * 20
OCR_BACKGROUND_COLOR_DISTANCE = 32
OCR_BACKGROUND_DARKEN_FACTOR = 0.5


class OCRRuntimeMixin:
    def _initialize_ocr_runtime(self) -> None:
        self._ocr_capture_task: asyncio.Task | None = None
        self._last_ocr_valid_strings: list[str | None] = [None] * OCR_REGION_COUNT
        self._last_ocr_observations: list[str | None] = [None] * OCR_REGION_COUNT
        self._previous_ocr_tokens: list[list[str] | None] = [None] * OCR_REGION_COUNT

    def _reset_ocr_runtime(self) -> None:
        self._last_ocr_valid_strings = [None] * OCR_REGION_COUNT
        self._last_ocr_observations = [None] * OCR_REGION_COUNT
        self._previous_ocr_tokens = [None] * OCR_REGION_COUNT

    @staticmethod
    def _read_ocr_crops(tesseract_path: str) -> list[list[str]]:
        pytesseract.pytesseract.tesseract_cmd = tesseract_path
        crop_directory = Path(__file__).resolve().parent / "ocr_crops"
        results: list[list[str]] = []
        for index in range(1, OCR_REGION_COUNT + 1):
            with Image.open(crop_directory / f"region_{index}.png") as crop:
                processed_crop = OCRRuntimeMixin._crop_to_dialogue_box(crop)
                processed_crop = OCRRuntimeMixin._remove_border_connected_white_pixels(processed_crop)
                processed_crop = OCRRuntimeMixin._crop_to_white_text_bounds(processed_crop)
                # processed_crop = OCRRuntimeMixin._crop_to_dialogue_box(processed_crop)
                processed_crop = OCRRuntimeMixin._darken_top_row_color(processed_crop)
                grayscale_image = ImageOps.grayscale(processed_crop)
                contrasted_image = ImageOps.autocontrast(grayscale_image)
                image = ImageOps.invert(contrasted_image)

                scaled_image = image.resize(
                    (image.width * 2, image.height * 2),
                    resample=Image.Resampling.LANCZOS,
                )

                scaled_image.save(
                    crop_directory / f"region_{index}_processed.png",
                    format="PNG",
                )
                data = pytesseract.image_to_data(
                    scaled_image,
                    config="--psm 6",
                    output_type=pytesseract.Output.DICT,
                )

            tokens: list[str] = []
            for token, confidence in zip(data["text"], data["conf"]):
                try:
                    confidence_value = float(confidence)
                except (TypeError, ValueError):
                    continue
                token = token.strip()
                if len(token) <= 1 and token not in ("|", "I", "A", "a", "T", "X", "-", "—"):
                    continue
                if token and confidence_value >= 40:
                    if token[:2] == "tt":
                        token = "it" + token[2:]
                    tokens.append(token.replace("|", "I").replace("VV", "W"))
            for i, token in enumerate(tokens):
                if ":" in token:
                    if i > 0:
                        tokens = tokens[i - 1:]
                    break
            results.append(tokens)

        return results

    @staticmethod
    def _crop_to_dialogue_box(image: Image.Image) -> Image.Image:
        rgb_image = image.convert("RGB")
        red, green, blue = rgb_image.split()
        green_delta = ImageChops.subtract(green, red)
        blue_delta = ImageChops.subtract(blue, red)
        green_teal = green_delta.point(
            lambda value: 255 if value >= OCR_DIALOGUE_BORDER_COLOR_DELTA else 0
        )
        blue_teal = blue_delta.point(
            lambda value: 255 if value >= OCR_DIALOGUE_BORDER_COLOR_DELTA else 0
        )
        teal_mask = ImageChops.darker(green_teal, blue_teal)
        teal_brightness = ImageChops.lighter(green, blue).point(
            lambda value: 255 if 35 <= value <= OCR_DIALOGUE_BORDER_MAX_CHANNEL else 0
        )
        channel_delta = ImageChops.difference(green, blue).point(
            lambda value: 255 if value <= OCR_DIALOGUE_BORDER_MAX_CHANNEL_DELTA else 0
        )
        combined_mask = ImageChops.darker(
            ImageChops.darker(teal_mask, teal_brightness),
            channel_delta,
        )

        width, height = image.size
        mask_pixels = combined_mask.load()
        minimum_line_width = max(80, int(width * 0.04))
        maximum_segment_gap = max(
            12,
            min(OCR_DIALOGUE_BORDER_MAX_SEGMENT_GAP, int(width * 0.04)),
        )
        line_tracks: list[list[tuple[int, int, int, int]]] = []
        for y in range(height):
            run_start: int | None = None
            last_hit = -1
            hit_count = 0
            row_lines: list[tuple[int, int, int]] = []
            for x in range(width):
                if mask_pixels[x, y] != 255:
                    continue
                if run_start is None or x - last_hit > maximum_segment_gap:
                    if run_start is not None:
                        row_lines.append((run_start, last_hit + 1, hit_count))
                    run_start = x
                    hit_count = 0
                last_hit = x
                hit_count += 1
            if run_start is not None:
                row_lines.append((run_start, last_hit + 1, hit_count))

            matched_tracks: set[int] = set()
            for line_start, line_end, line_hits in row_lines:
                if line_end - line_start >= minimum_line_width and line_hits >= (line_end - line_start) * 0.3:
                    matching_tracks = []
                    for track_index, track in enumerate(line_tracks):
                        if track_index in matched_tracks or y - track[-1][0] > 2:
                            continue
                        _, track_start, track_end, _ = track[-1]
                        overlap = min(line_end, track_end) - max(line_start, track_start)
                        shorter_line = min(line_end - line_start, track_end - track_start)
                        if overlap >= shorter_line * 0.5:
                            matching_tracks.append((overlap / shorter_line, track_index))

                    if matching_tracks:
                        _, track_index = max(matching_tracks)
                        line_tracks[track_index].append((y, line_start, line_end, line_hits))
                        matched_tracks.add(track_index)
                    else:
                        line_tracks.append([(y, line_start, line_end, line_hits)])
                        matched_tracks.add(len(line_tracks) - 1)

        horizontal_lines = [
            (
                track[0][0],
                track[-1][0],
                min(line[1] for line in track),
                max(line[2] for line in track),
                max(line[3] for line in track),
            )
            for track in line_tracks
            if len(track) >= 2
        ]

        best_bounds: tuple[int, int, int, int] | None = None
        best_score = 0.0
        for top_index, (top, top_end, top_left, top_right, top_hits) in enumerate(horizontal_lines):
            for bottom, bottom_end, bottom_left, bottom_right, bottom_hits in horizontal_lines[top_index + 1:]:
                interior_height = bottom - top_end
                if interior_height < max(12, int(height * 0.12)):
                    continue
                overlap = min(top_right, bottom_right) - max(top_left, bottom_left)
                shorter_line = min(top_right - top_left, bottom_right - bottom_left)
                if overlap < shorter_line * 0.5:
                    continue

                left = min(top_left, bottom_left)
                right = max(top_right, bottom_right)
                box_width = right - left
                side_search_radius = max(8, int(box_width * 0.06))
                left_pixels, left_side = max(
                    (
                        sum(mask_pixels[x, y] == 255 for y in range(top_end + 1, bottom)),
                        x,
                    )
                    for x in range(max(0, left - side_search_radius), min(width, left + side_search_radius + 1))
                )
                right_pixels, right_side = max(
                    (
                        sum(mask_pixels[x, y] == 255 for y in range(top_end + 1, bottom)),
                        x,
                    )
                    for x in range(max(0, right - side_search_radius), min(width, right + side_search_radius + 1))
                )
                minimum_side_pixels = max(2, int(interior_height * 0.08))
                if left_pixels < minimum_side_pixels or right_pixels < minimum_side_pixels:
                    continue

                score = (
                    overlap
                    * min(left_pixels, right_pixels)
                    * min(top_hits, bottom_hits)
                )
                if score > best_score:
                    best_score = score
                    best_bounds = (
                        min(left, left_side),
                        top,
                        max(right, right_side + 1),
                        bottom_end + 1,
                    )

        for channel in (
            red,
            green,
            blue,
            green_delta,
            blue_delta,
            green_teal,
            blue_teal,
            teal_mask,
            teal_brightness,
            channel_delta,
            combined_mask,
            rgb_image,
        ):
            channel.close()

        if best_bounds is None:
            return image.copy()

        left, top, right, bottom = best_bounds
        return image.crop((
            max(0, left - OCR_DIALOGUE_BORDER_PADDING),
            max(0, top - OCR_DIALOGUE_BORDER_PADDING),
            min(width, right + OCR_DIALOGUE_BORDER_PADDING),
            min(height, bottom + OCR_DIALOGUE_BORDER_PADDING),
        ))

    @staticmethod
    def _remove_border_connected_white_pixels(image: Image.Image) -> Image.Image:
        rgb_image = image.convert("RGB")
        channels = rgb_image.split()
        bright_channels = [
            channel.point(
                lambda value: 255 if value >= OCR_WHITE_PIXEL_THRESHOLD else 0
            )
            for channel in channels
        ]
        white_mask = ImageChops.darker(
            ImageChops.darker(bright_channels[0], bright_channels[1]),
            bright_channels[2],
        )
        width, height = image.size
        mask_pixels = white_mask.load()
        border_points = {
            (x, y)
            for x in range(width)
            for y in range(min(OCR_BORDER_SEARCH_DISTANCE + 1, height))
        }
        border_points.update(
            (x, y)
            for x in range(width)
            for y in range(max(0, height - OCR_BORDER_SEARCH_DISTANCE - 1), height)
        )
        border_points.update(
            (x, y)
            for y in range(height)
            for x in range(min(OCR_BORDER_SEARCH_DISTANCE + 1, width))
        )
        border_points.update(
            (x, y)
            for y in range(height)
            for x in range(max(0, width - OCR_BORDER_SEARCH_DISTANCE - 1), width)
        )
        pending = deque(
            point for point in border_points
            if mask_pixels[point] == 255
        )
        for x, y in pending:
            mask_pixels[x, y] = 128

        while pending:
            x, y = pending.popleft()
            for neighbor_y in range(max(0, y - 3), min(height, y + 4)):
                for neighbor_x in range(max(0, x - 3), min(width, x + 4)):
                    if mask_pixels[neighbor_x, neighbor_y] == 255:
                        mask_pixels[neighbor_x, neighbor_y] = 128
                        pending.append((neighbor_x, neighbor_y))

        border_mask = white_mask.point(lambda value: 255 if value == 128 else 0)
        black_image = Image.new("RGB", rgb_image.size, (0, 0, 0))
        cleaned_image = Image.composite(black_image, rgb_image, border_mask)

        for channel in channels:
            channel.close()
        for channel in bright_channels:
            channel.close()
        white_mask.close()
        border_mask.close()
        black_image.close()
        rgb_image.close()
        return cleaned_image

    @staticmethod
    def _crop_to_white_text_bounds(image: Image.Image) -> Image.Image:
        rgb_image = image.convert("RGB")
        channels = rgb_image.split()
        bright_channels = [
            channel.point(
                lambda value: 255 if value >= OCR_WHITE_PIXEL_THRESHOLD else 0
            )
            for channel in channels
        ]
        white_mask = ImageChops.darker(
            ImageChops.darker(bright_channels[0], bright_channels[1]),
            bright_channels[2],
        )
        bounds = white_mask.getbbox()

        for channel in channels:
            channel.close()
        for channel in bright_channels:
            channel.close()
        white_mask.close()
        rgb_image.close()

        if bounds is None:
            return image.copy()

        left, top, right, bottom = bounds
        crop_left = max(0, left - OCR_TEXT_CROP_LEFT_BUFFER)
        crop_top = max(0, top - OCR_TEXT_CROP_BUFFER)
        crop_bottom = min(image.height, bottom + OCR_TEXT_CROP_BUFFER)
        crop_right = min(image.width, right + OCR_TEXT_CROP_BUFFER)
        return image.crop((crop_left, crop_top, crop_right, crop_bottom))

    @staticmethod
    def _darken_top_row_color(image: Image.Image) -> Image.Image:
        rgb_image = image.convert("RGB")
        top_row = rgb_image.crop((0, 0, rgb_image.width, 1))
        background_color = tuple(ImageStat.Stat(top_row).median)
        background_image = Image.new("RGB", rgb_image.size, background_color)
        difference_channels = ImageChops.difference(rgb_image, background_image).split()
        color_distance = ImageChops.lighter(
            ImageChops.lighter(difference_channels[0], difference_channels[1]),
            difference_channels[2],
        )
        background_mask = color_distance.point(
            lambda distance: 255 if distance <= OCR_BACKGROUND_COLOR_DISTANCE else 0
        )
        darkened_image = ImageEnhance.Brightness(rgb_image).enhance(OCR_BACKGROUND_DARKEN_FACTOR)
        adjusted_image = Image.composite(darkened_image, rgb_image, background_mask)

        top_row.close()
        background_image.close()
        for channel in difference_channels:
            channel.close()
        color_distance.close()
        background_mask.close()
        darkened_image.close()
        rgb_image.close()
        return adjusted_image

    def _stable_ocr_prefix(self, region_index: int, tokens: list[str]) -> str | None:
        start = None
        for index, token in enumerate(tokens):
            if ":" in token:
                start = index - 1 if token == ":" and index > 0 else index
                break
        if start is None:
            self._previous_ocr_tokens[region_index] = None
            return None

        current_tokens = tokens[start:]
        previous_tokens = self._previous_ocr_tokens[region_index]
        self._previous_ocr_tokens[region_index] = current_tokens
        if previous_tokens is None:
            return None

        stable_count = 0
        for previous, current in zip(previous_tokens, current_tokens):
            if previous.casefold() != current.casefold():
                break
            stable_count += 1

        if stable_count == 0:
            return None
        return " ".join(current_tokens[:stable_count])

    def _filter_ocr_text(self, region_index: int, recognized_text: str) -> str | None:
        text = recognized_text.strip()
        name, separator, message = text.partition(":")
        name = name.strip()
        message = message.strip()
        message_ending = message.rstrip("\"'”’()[]\{\}»1234567890_~=+*/\\|@#$%^&<>")
        if (
            not separator
            or not name
            or not message
            or not any(character.isalpha() for character in name)
            or not any(character.isalnum() for character in message)
            or not message_ending
            or message_ending[-1] not in ".,:;?!…-—"
            or "<" in message or ">" in message or "=" in message
        ):
            return None

        if name.casefold().startswith("player") or name.casefold().startswith(self._display_name.casefold()):
            return None

        current = f"{name}: {message}"
        previous_valid = self._last_ocr_valid_strings[region_index]
        if previous_valid is not None:
            if self._ocr_strings_are_similar(previous_valid, current):
                return None

            previous_message = previous_valid.partition(":")[2].strip()
            if self._ocr_text_is_contained(message, previous_message):
                return None

        previous = self._last_ocr_observations[region_index]
        self._last_ocr_observations[region_index] = current
        current_tokens = current.split()

        if previous is None:
            self._last_ocr_valid_strings[region_index] = current
            return current
        previous_tokens = previous.split()
        previous_token_count = len(previous_tokens)

        if (
            len(current_tokens) >= previous_token_count
            and self._ocr_tokens_are_similar(
                current_tokens[:previous_token_count],
                previous_tokens,
            )
        ):
            appended_text = " ".join(current_tokens[previous_token_count:])
            self._last_ocr_valid_strings[region_index] = current
            return f"{name}: {appended_text}" if appended_text else None

        if (
            len(current_tokens) >= previous_token_count
            and self._ocr_tokens_are_similar(
                current_tokens[-previous_token_count:],
                previous_tokens,
            )
        ):
            return None

        self._last_ocr_valid_strings[region_index] = current
        return current

    @classmethod
    def _ocr_text_is_contained(cls, candidate: str, previous: str) -> bool:
        candidate_tokens = re.sub(r"[^\w'’]+", " ", candidate.casefold()).split()
        previous_tokens = re.sub(r"[^\w'’]+", " ", previous.casefold()).split()
        if not candidate_tokens or len(candidate_tokens) > len(previous_tokens):
            return False

        previous_index = 0
        for candidate_token in candidate_tokens:
            while previous_index < len(previous_tokens) and not cls._ocr_tokens_are_similar(
                [previous_tokens[previous_index]],
                [candidate_token],
            ):
                previous_index += 1
            if previous_index == len(previous_tokens):
                return False
            previous_index += 1

        return True

    @staticmethod
    def _ocr_strings_are_similar(first: str, second: str) -> bool:
        normalized_first = " ".join(first.casefold().split())
        normalized_second = " ".join(second.casefold().split())
        return SequenceMatcher(
            None,
            normalized_first,
            normalized_second,
            autojunk=False,
        ).ratio() >= 0.94

    @staticmethod
    def _ocr_tokens_are_similar(first: list[str], second: list[str]) -> bool:
        if len(first) != len(second):
            return False

        for first_token, second_token in zip(first, second):
            normalized_first = first_token.casefold().strip(".,:;?!…\"'’‘”“)]}»([{")
            normalized_second = second_token.casefold().strip(".,:;?!…\"'’‘”“)]}»([{")
            if not normalized_first or not normalized_second:
                if normalized_first == normalized_second:
                    continue
                return False
            if normalized_first == normalized_second:
                continue
            if min(len(normalized_first), len(normalized_second)) < 4:
                return False
            if SequenceMatcher(
                None,
                normalized_first,
                normalized_second,
                autojunk=False,
            ).ratio() < 0.8:
                return False

        return True

    @staticmethod
    def _save_ocr_crops(regions: list[tuple[float, float, float, float]]) -> None:
        output_directory = Path(__file__).resolve().parent / "ocr_crops"
        output_directory.mkdir(parents=True, exist_ok=True)

        screenshot = ImageGrab.grab()
        try:
            screenshot_width, screenshot_height = screenshot.size
            for index, (x, y, width, height) in enumerate(regions, start=1):
                left = round(x * screenshot_width)
                top = round(y * screenshot_height)
                right = round((x + width) * screenshot_width)
                bottom = round((y + height) * screenshot_height)
                crop = screenshot.crop((left, top, right, bottom))

                output_path = output_directory / f"region_{index}.png"
                temporary_path = output_directory / f".region_{index}.{uuid.uuid4().hex}.tmp.png"
                try:
                    crop.save(temporary_path, format="PNG")
                    for attempt in range(4):
                        try:
                            temporary_path.replace(output_path)
                            break
                        except OSError as exc:
                            if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 3:
                                raise
                            time.sleep(0.05 * (attempt + 1))
                finally:
                    crop.close()
                    temporary_path.unlink(missing_ok=True)
        finally:
            screenshot.close()
