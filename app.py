*** Begin Patch
*** Update File: app.py
@@
     def create_schedule(patient_id: int):
         actor = current_user()
         patient = get_patient(patient_id)
         data = request.get_json(silent=True) or {}
+        if not isinstance(data, dict):
+            return json_error("request body must be an object", 400)
         med_name, reminder_time, dosage = data.get("med_name"), data.get("time"), data.get("dosage")
         compartment = data.get("compartment")
         if isinstance(compartment, str):
             compartment = compartment.strip() or None
         tablet_weight = data.get("tablet_weight", 0.5)
@@
         values, error = normalize_medication_data({
             "name": med_name,
             "time": reminder_time,
             "dosage": dosage,
             "compartment": compartment,
             "tablet_weight": tablet_weight,
             "expected_quantity": expected_quantity,
             "tolerance": tolerance,
             "calibration_offset": calibration_offset,
             "response_window_minutes": response_window_minutes,
             "noise_threshold": noise_threshold,
         }, "ADD")
@@
     def update_schedule(patient_id: int, reminder_id: int):
         actor = current_user()
         patient = get_patient(patient_id)
         if patient is None:
             return json_error("patient not found", 404)
@@
         if reminder is None or reminder.status != "Active":
             return json_error("active schedule not found", 404)
         data = request.get_json(silent=True) or {}
-        allowed_fields = {"med_name", "time", "dosage", "compartment"}
-        if not isinstance(data, dict) or set(data) - allowed_fields:
-            return json_error("schedule data contains unsupported fields", 400)
+        if not isinstance(data, dict):
+            return json_error("request body must be an object", 400)
+        allowed_fields = {"med_name", "time", "dosage", "compartment"}
+        filtered = {key: value for key, value in data.items() if key in allowed_fields}
+        if not filtered:
+            return json_error("a schedule change is required", 400)
         values = {
             {"med_name": "name"}.get(field, field): value
-            for field, value in data.items()
+            for field, value in filtered.items()
         }
         if isinstance(values.get("compartment"), str):
             values["compartment"] = values["compartment"].strip() or None
*** End Patch