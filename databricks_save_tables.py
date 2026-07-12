# # =============================================================================
# # CELL: Save Department KPI DataFrames as Databricks Delta Tables
# # Phase 2 — includes has_active_flag from flags table
# # =============================================================================
# # Replace your old save cell with this one entirely.
# # Run immediately after run_aviation_pipeline() on every refresh.
# # NOTE: In Databricks, 'spark' is already available — never create it manually.
# # =============================================================================

# from pyspark.sql.functions import current_timestamp, col, lit

# SCHEMA_NAME = "ojas_aviation"

# DEPT_TABLE_MAP = {
#     "CNC":          "dept_cnc",
#     "VMC":          "dept_vmc",
#     "CONVENTIONAL": "dept_conventional",
#     "SHEET METAL":  "dept_sheet_metal",
#     "PRODUCTION":   "dept_production",
#     "EDM":          "dept_edm",
# }

# # -----------------------------------------------------------------------------
# # Step 1: Create schema if it does not already exist
# # 'spark' is already available in Databricks — no import or creation needed.
# # -----------------------------------------------------------------------------
# spark.sql(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA_NAME}")
# print(f"✓ Schema '{SCHEMA_NAME}' is ready.\n")

# # -----------------------------------------------------------------------------
# # Step 2: Run the pipeline
# # wos_df and ows_df must already be loaded above in the notebook.
# # run_aviation_pipeline() must already be defined above in the notebook.
# # -----------------------------------------------------------------------------
# results = run_aviation_pipeline(wos_df, ows_df)

# # -----------------------------------------------------------------------------
# # Step 3: Build the active-flags lookup
# #
# # Reads the flags table and finds which wo_ids have flag_status = 1.
# # Uses MAX(flag_status) per wo_id:
# #   MAX = 1 → at least one active flag → has_active_flag = True
# #   MAX = 0 → all flags resolved       → has_active_flag = False
# #   WO not in flags table at all       → LEFT JOIN gives null → filled False
# #
# # The try/except handles the first run where the flags table does not
# # exist yet. All WOs get has_active_flag = False in that case, which is
# # correct — no flags have been raised yet.
# # -----------------------------------------------------------------------------
# try:
#     active_flags_df = spark.sql(f"""
#         SELECT
#             wo_id,
#             MAX(flag_status) AS has_active_flag
#         FROM {SCHEMA_NAME}.flags
#         GROUP BY wo_id
#     """)

#     # Convert integer 1/0 to boolean True/False
#     # FastAPI Pydantic model expects a boolean, not an integer
#     active_flags_df = active_flags_df.withColumn(
#         "has_active_flag",
#         col("has_active_flag") == 1
#     )

#     flag_count = active_flags_df.filter(col("has_active_flag") == True).count()
#     print(f"✓ Flags lookup built. {flag_count} WO(s) currently have an active flag.\n")

# except Exception as e:
#     # Normal on first run — flags table does not exist yet
#     print(f"  ⚠  Flags table not found (normal on first run): {e}")
#     print(f"  ⚠  All WOs will have has_active_flag = False\n")
#     active_flags_df = None

# # -----------------------------------------------------------------------------
# # Step 4: Write each department DataFrame to its Delta table
# #
# # For each department:
# #   a) Convert Pandas → Spark DataFrame
# #   b) LEFT JOIN with active_flags_df on wo_id
# #      LEFT JOIN = every WO row is kept even if it has no flag entry
# #   c) Fill nulls with False for WOs not in the flags table
# #   d) Add last_refreshed timestamp
# #   e) Overwrite the Delta table completely (full refresh every time)
# # -----------------------------------------------------------------------------
# for dept_name, dept_df in results.items():

#     table_name      = DEPT_TABLE_MAP[dept_name]
#     full_table_name = f"{SCHEMA_NAME}.{table_name}"

#     if dept_df.empty:
#         print(f"  ⚠  {dept_name} — 0 records. Empty table will still be created.")

#     # a) Convert Pandas → Spark
#     spark_df = spark.createDataFrame(dept_df)

#     # b) Join flags lookup
#     if active_flags_df is not None:
#         spark_df = spark_df.join(active_flags_df, on="wo_id", how="left")
#         # c) Fill nulls — WOs with no flag entry get False
#         spark_df = spark_df.fillna({"has_active_flag": False})
#     else:
#         # Flags table unavailable — add column as all False
#         spark_df = spark_df.withColumn("has_active_flag", lit(False))

#     # d) Add refresh timestamp
#     spark_df = spark_df.withColumn("last_refreshed", current_timestamp())

#     # e) Overwrite Delta table
#     (
#         spark_df
#         .write
#         .format("delta")
#         .mode("overwrite")
#         .option("overwriteSchema", "true")
#         .saveAsTable(full_table_name)
#     )

#     print(f"  ✓  {dept_name:15s} → {full_table_name}  ({len(dept_df)} records)")

# print("\n✅ All 6 department tables saved successfully.")
# print(f"   Schema : {SCHEMA_NAME}")
# print(f"   Tables : {', '.join(DEPT_TABLE_MAP.values())}")

# # -----------------------------------------------------------------------------
# # Verification — preview one table to confirm write succeeded
# # -----------------------------------------------------------------------------
# print("\n--- Preview: ojas_aviation.dept_cnc (3 rows) ---")
# spark.sql("""
#     SELECT wo_id, status, has_active_flag, last_refreshed
#     FROM ojas_aviation.dept_cnc
#     LIMIT 3
# """).show(truncate=False)