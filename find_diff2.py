import re

values = set()

# Check interview_service
with open('backend/services/interview_service.py') as f:
    content = f.read()
matches = re.findall(r'["\'](medium|hard|easy|technical|behavioral|beginner)["\']', content, re.IGNORECASE)
values.update(matches)

# Check interview_generator
with open('backend/services/interview_generator.py') as f:
    content = f.read()
matches = re.findall(r'["\'](medium|hard|easy|technical|behavioral|beginner)["\']', content, re.IGNORECASE)
values.update(matches)

# Check interview_page
with open('frontend/user/interview_page.py') as f:
    content = f.read()
matches = re.findall(r'["\'](medium|hard|easy|technical|behavioral)["\']', content, re.IGNORECASE)
values.update(matches)

print("Difficulty values found:", sorted(values))