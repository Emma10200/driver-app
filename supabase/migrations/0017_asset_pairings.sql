-- Pre-computed asset pairings timeline.
-- Stores truck↔trailer assignment periods computed from GPS co-location evidence.
-- This table makes the Unit Timeline instant (simple SELECT instead of heavy computation).
--
-- Run this in the Supabase SQL Editor.

CREATE TABLE IF NOT EXISTS asset_pairings (
    id          bigserial       PRIMARY KEY,
    truck_id    text            NOT NULL,
    trailer_id  text            NOT NULL,
    start_time  timestamptz     NOT NULL,
    end_time    timestamptz,
    duration_minutes float,
    avg_distance_miles float,
    confidence  float           DEFAULT 0,
    bucket_count int            DEFAULT 0,
    ended_by    text,           -- 'yard_entry', 'new_pairing', 'signal_loss', 'manual'
    division    text            DEFAULT '',
    computed_at timestamptz     DEFAULT now(),
    source      text            DEFAULT 'auto'  -- 'auto', 'manual', 'backfill'
);

-- Indexes for fast timeline queries
CREATE INDEX IF NOT EXISTS idx_pairings_truck ON asset_pairings (truck_id, start_time DESC);
CREATE INDEX IF NOT EXISTS idx_pairings_trailer ON asset_pairings (trailer_id, start_time DESC);
CREATE INDEX IF NOT EXISTS idx_pairings_time ON asset_pairings (start_time, end_time);

-- RLS: allow service role full access
ALTER TABLE asset_pairings ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_all" ON asset_pairings
    FOR ALL USING (true) WITH CHECK (true);

COMMENT ON TABLE asset_pairings IS 'Pre-computed truck↔trailer assignment timeline from GPS co-location. Populated by backfill script or periodic computation.';
