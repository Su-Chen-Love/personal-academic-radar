CREATE TABLE `cloud_feedback` (
	`seq` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`identity` text NOT NULL,
	`interest` text,
	`reason` text,
	`favorite` integer NOT NULL,
	`reading_status` text NOT NULL,
	`updated_at` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `generations` (
	`id` text PRIMARY KEY NOT NULL,
	`count` integer NOT NULL,
	`created_at` text NOT NULL,
	`status` text DEFAULT 'staging' NOT NULL
);
--> statement-breakpoint
CREATE TABLE `pointers` (
	`key` text PRIMARY KEY NOT NULL,
	`value` text NOT NULL
);
--> statement-breakpoint
CREATE TABLE `records` (
	`generation` text NOT NULL,
	`kind` text NOT NULL,
	`key` text NOT NULL,
	`data` text NOT NULL,
	`checksum` text NOT NULL,
	PRIMARY KEY(`generation`, `kind`, `key`)
);
