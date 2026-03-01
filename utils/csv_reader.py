import csv
from typing import List
from pathlib import Path
import pandas as pd
from pydantic import ValidationError

from models.test_case import TestCase, TestPriority, TestCategory
from utils.logger import log


class CSVTestCaseReader:
    """Read and parse test cases from CSV files"""
    
    def __init__(self, csv_path: Path):
        self.csv_path = csv_path
    
    def read_test_cases(self) -> List[TestCase]:
        """Read test cases from CSV file"""
        
        if not self.csv_path.exists():
            raise FileNotFoundError(f"Test cases file not found: {self.csv_path}")
        
        log.info(f"📂 Reading test cases from: {self.csv_path}")
        
        try:
            # Read CSV using pandas for better handling
            df = pd.read_csv(self.csv_path)
            
            # Validate required columns
            required_columns = ['test_id', 'test_name', 'description', 'expected_result']
            missing_columns = [col for col in required_columns if col not in df.columns]
            
            if missing_columns:
                raise ValueError(f"Missing required columns: {missing_columns}")
            
            # Parse test cases
            test_cases = []
            errors = []
            
            for index, row in df.iterrows():
                try:
                    test_case = self._parse_test_case(row)
                    test_cases.append(test_case)
                except Exception as e:
                    errors.append(f"Row {index + 2}: {str(e)}")
            
            # Report parsing results
            log.info(f"✅ Successfully parsed {len(test_cases)} test cases")
            
            if errors:
                log.warning(f"⚠️  {len(errors)} test cases failed to parse:")
                for error in errors:
                    log.warning(f"   - {error}")
            
            if not test_cases:
                raise ValueError("No valid test cases found in CSV")
            
            return test_cases
        
        except Exception as e:
            log.error(f"❌ Error reading CSV file: {e}")
            raise
    
    def _parse_test_case(self, row: pd.Series) -> TestCase:
        """Parse a single test case from CSV row"""
        
        # Clean and extract data
        test_id = str(row['test_id']).strip()
        test_name = str(row['test_name']).strip()
        description = str(row['description']).strip()
        expected_result = str(row['expected_result']).strip()
        
        # Optional fields with defaults
        priority = self._parse_priority(row.get('priority', 'Medium'))
        category = self._parse_category(row.get('category', 'Functionality Test'))
        
        # Parse tags (comma-separated)
        tags = []
        if 'tags' in row and pd.notna(row['tags']):
            tags = [tag.strip() for tag in str(row['tags']).split(',')]
        
        # Retry configuration
        retry_on_failure = True
        if 'retry_on_failure' in row and pd.notna(row['retry_on_failure']):
            retry_str = str(row['retry_on_failure']).lower()
            retry_on_failure = retry_str in ['true', 'yes', '1', 'y']
        
        # Timeout
        timeout = None
        if 'timeout' in row and pd.notna(row['timeout']):
            try:
                timeout = int(row['timeout'])
            except ValueError:
                pass
        
        # Created by
        created_by = None
        if 'created_by' in row and pd.notna(row['created_by']):
            created_by = str(row['created_by']).strip()
        
        # Create test case
        try:
            test_case = TestCase(
                test_id=test_id,
                test_name=test_name,
                description=description,
                priority=priority,
                category=category,
                expected_result=expected_result,
                tags=tags,
                retry_on_failure=retry_on_failure,
                timeout=timeout,
                created_by=created_by
            )
            return test_case
        except ValidationError as e:
            raise ValueError(f"Validation error for test {test_id}: {e}")
    
    def _parse_priority(self, priority_str: str) -> TestPriority:
        """Parse priority from string"""
        priority_map = {
            'critical': TestPriority.CRITICAL,
            'high': TestPriority.HIGH,
            'medium': TestPriority.MEDIUM,
            'low': TestPriority.LOW
        }
        
        priority_lower = str(priority_str).lower().strip()
        return priority_map.get(priority_lower, TestPriority.MEDIUM)
    
    def _parse_category(self, category_str: str) -> TestCategory:
        """Parse category from string"""
        category_map = {
            'smoke': TestCategory.SMOKE,
            'smoke test': TestCategory.SMOKE,
            'regression': TestCategory.REGRESSION,
            'functionality': TestCategory.FUNCTIONALITY,
            'functionality test': TestCategory.FUNCTIONALITY,
            'integration': TestCategory.INTEGRATION,
            'e2e': TestCategory.E2E,
            'end-to-end': TestCategory.E2E
        }
        
        category_lower = str(category_str).lower().strip()
        return category_map.get(category_lower, TestCategory.FUNCTIONALITY)
    
    @staticmethod
    def validate_csv_format(csv_path: Path) -> tuple[bool, List[str]]:
        """Validate CSV format without loading all data"""
        
        errors = []
        
        if not csv_path.exists():
            errors.append(f"File not found: {csv_path}")
            return False, errors
        
        try:
            df = pd.read_csv(csv_path, nrows=1)
            
            # Check required columns
            required_columns = ['test_id', 'test_name', 'description', 'expected_result']
            missing = [col for col in required_columns if col not in df.columns]
            
            if missing:
                errors.append(f"Missing required columns: {', '.join(missing)}")
            
            # Check for empty file
            if len(df) == 0:
                errors.append("CSV file is empty")
            
            return len(errors) == 0, errors
        
        except Exception as e:
            errors.append(f"Error reading CSV: {str(e)}")
            return False, errors


def generate_sample_csv(output_path: Path):
    """Generate a sample test_cases.csv file"""
    
    sample_data = [
        {
            'test_id': 'TC-001',
            'test_name': 'Duplicate Institution ID Validation',
            'description': '''Test Objective: Verify duplicate institution ID prevention in i2i Connect

Test Steps:
1. Navigate to https://dev2.heartnetnetindiademo.in/connect
2. Log in with username "admini2i" and password "connect@sadmin"
3. Wait 3 seconds for dashboard
4. Click Admin button (has admin icon)
5. Click "Add" from menu
6. Wait 3 seconds for form
7. Fill institution form:
   - Institution Name: nasanapptest
   - User Name (Institution ID): nasanapptest
   - Password: Connect@2024
   - Confirm Password: Connect@2024
   - Address1: addr_1
   - Address2: addr_2
   - State: Maharashtra
   - City: Pune
   - Pincode: 411045
   - Country: India
   - Email: gopi@silfratech.com
   - Contact Number: 9876543210
   - Contact Person: Gopi
8. Click Save button
9. Wait 3 seconds
10. Verify error message

Expected: System prevents duplicate and shows error message''',
            'expected_result': 'Username already exists',
            'priority': 'High',
            'category': 'Functionality Test',
            'tags': 'duplicate-check,validation,institution',
            'retry_on_failure': 'true',
            'timeout': '',
            'created_by': 'QA Team'
        },
        {
            'test_id': 'TC-002',
            'test_name': 'Login with Valid Credentials',
            'description': '''Test Objective: Verify successful login with valid credentials

Test Steps:
1. Navigate to https://dev2.heartnetnetindiademo.in/connect
2. Enter username: admini2i
3. Enter password: connect@sadmin
4. Click login button
5. Wait for dashboard to load
6. Verify user is on dashboard page

Expected: User successfully logs in and dashboard is displayed''',
            'expected_result': 'dashboard',
            'priority': 'Critical',
            'category': 'Smoke Test',
            'tags': 'login,authentication,smoke',
            'retry_on_failure': 'true',
            'timeout': '60',
            'created_by': 'QA Team'
        },
        {
            'test_id': 'TC-003',
            'test_name': 'Add New Institution - Success',
            'description': '''Test Objective: Successfully add a new institution with unique ID

Test Steps:
1. Navigate to https://dev2.heartnetnetindiademo.in/connect
2. Log in with credentials
3. Navigate to Admin > Add Institution
4. Fill form with unique institution ID: testinst_{{timestamp}}
5. Fill all required fields with valid data
6. Submit form
7. Verify success message

Expected: Institution created successfully''',
            'expected_result': 'success',
            'priority': 'High',
            'category': 'Functionality Test',
            'tags': 'institution,add,positive-test',
            'retry_on_failure': 'true',
            'timeout': '',
            'created_by': 'QA Team'
        }
    ]
    
    df = pd.DataFrame(sample_data)
    df.to_csv(output_path, index=False)
    
    log.info(f"✅ Sample test cases CSV generated: {output_path}")