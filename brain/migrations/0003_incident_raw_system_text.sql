-- §8.5 alias learning through the pick/fix buttons: persist the transcript
-- window that named the system when the incident opened, so a button press —
-- which carries no transcript, and may land after a Brain restart — can still
-- write the (raw text → system) alias row.
ALTER TABLE incidents ADD COLUMN raw_system_text TEXT;
