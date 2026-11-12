SELECT trade_id, updated_at FROM trades WHERE updated_at > %(watermark)s
