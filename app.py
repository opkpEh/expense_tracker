from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, Response
from pymongo import MongoClient, DESCENDING
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import os
from bson.objectid import ObjectId
import csv
import io
import json
import functools
from pymongo.errors import PyMongoError

app = Flask(__name__)
app.secret_key = os.urandom(24)

# MongoDB Connection with connection pooling
client = MongoClient('mongodb://localhost:27017/', 
                     maxPoolSize=50,  # Increased connection pool size
                     connectTimeoutMS=5000,
                     serverSelectionTimeoutMS=5000)
db = client['calorie_tracker_db']
users_collection = db['users']
food_entries_collection = db['food_entries']

# Enhanced database indexes for better performance
users_collection.create_index('username', unique=True)
food_entries_collection.create_index('user_id')
food_entries_collection.create_index([('user_id', 1), ('date', 1)])  # Compound index for faster date queries
food_entries_collection.create_index([('date', 1), ('user_id', 1)])  # Additional index for date-range queries

# Simple cache decorator for performance on expensive operations
def simple_cache(timeout=300):
    """Simple in-memory cache decorator with timeout."""
    cache = {}
    
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            key = str(args) + str(kwargs)
            now = datetime.now()
            
            if key in cache:
                result, timestamp = cache[key]
                if (now - timestamp).total_seconds() < timeout:
                    return result
            
            result = func(*args, **kwargs)
            cache[key] = (result, now)
            return result
        return wrapper
    return decorator

# Routes
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        name = request.form['name']
        
        # Improved validation
        try:
            daily_goal = int(request.form['daily_goal'])
            if daily_goal < 500:
                flash('Daily goal must be at least 500 calories', 'error')
                return redirect(url_for('register'))
        except ValueError:
            flash('Daily goal must be a valid number', 'error')
            return redirect(url_for('register'))
        
        # Check if username already exists
        if users_collection.find_one({'username': username}):
            flash('Username already exists!', 'error')
            return redirect(url_for('register'))
        
        # Create new user
        new_user = {
            'username': username,
            'password': generate_password_hash(password),
            'name': name,
            'daily_goal': daily_goal,
            'created_at': datetime.now()
        }
        
        try:
            inserted_id = users_collection.insert_one(new_user).inserted_id
            
            session['user_id'] = str(inserted_id)
            session['username'] = username
            session['name'] = name
            session['daily_goal'] = daily_goal
            
            flash('Registration successful!', 'success')
            return redirect(url_for('dashboard'))
        except PyMongoError as e:
            flash(f'Database error: {str(e)}', 'error')
            return redirect(url_for('register'))
    
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        try:
            user = users_collection.find_one({'username': username})
            
            if user and check_password_hash(user['password'], password):
                session['user_id'] = str(user['_id'])
                session['username'] = user['username']
                session['name'] = user['name']
                session['daily_goal'] = user['daily_goal']
                
                flash('Login successful!', 'success')
                return redirect(url_for('dashboard'))
            else:
                flash('Invalid username or password', 'error')
        except PyMongoError as e:
            flash(f'Database error: {str(e)}', 'error')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out', 'success')
    return redirect(url_for('index'))

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        flash('Please log in first', 'error')
        return redirect(url_for('login'))
    
    today = datetime.now().strftime('%Y-%m-%d')
    
    try:
        # Get today's entries
        entries = list(food_entries_collection.find({
            'user_id': session['user_id'],
            'date': today
        }).sort('created_at', DESCENDING))
        
        # Calculate totals
        total_calories = sum(entry['calories'] for entry in entries)
        calories_left = session['daily_goal'] - total_calories
        
        # Get meal type breakdown for today
        meal_breakdown = get_meal_breakdown(session['user_id'], today)
        
        # Get week data for chart
        week_data = get_week_data(session['user_id'])
        
        # Get monthly data for heatmap
        monthly_data = get_monthly_data(session['user_id'])
        
        return render_template('dashboard.html', 
                            entries=entries, 
                            total_calories=total_calories, 
                            calories_left=calories_left,
                            daily_goal=session['daily_goal'],
                            name=session['name'],
                            date=today,
                            meal_breakdown=meal_breakdown,
                            week_data=json.dumps(week_data),
                            monthly_data=json.dumps(monthly_data))
    except PyMongoError as e:
        flash(f'Database error: {str(e)}', 'error')
        return render_template('dashboard.html', 
                            entries=[], 
                            total_calories=0, 
                            calories_left=session['daily_goal'],
                            daily_goal=session['daily_goal'],
                            name=session['name'],
                            date=today,
                            meal_breakdown={'breakfast': 0, 'lunch': 0, 'dinner': 0, 'snack': 0},
                            week_data=json.dumps([]),
                            monthly_data=json.dumps([]))

@simple_cache(timeout=300)  # Cache for 5 minutes
def get_meal_breakdown(user_id, date):
    """Get calorie breakdown by meal type for a specific date - Optimized to single aggregation"""
    pipeline = [
        {'$match': {'user_id': user_id, 'date': date}},
        {'$group': {
            '_id': '$meal_type',
            'total': {'$sum': '$calories'}
        }},
        {'$project': {
            '_id': 0,
            'meal_type': '$_id',
            'total': 1
        }}
    ]
    
    result = list(food_entries_collection.aggregate(pipeline))
    
    # Convert to dictionary for easier use in template
    breakdown = {
        'breakfast': 0,
        'lunch': 0,
        'dinner': 0,
        'snack': 0
    }
    
    for item in result:
        breakdown[item['meal_type']] = item['total']
        
    return breakdown

@simple_cache(timeout=600)  # Cache for 10 minutes
def get_week_data(user_id):
    """Get daily calorie totals for the past week with improved analytics"""
    # Calculate date range
    end_date = datetime.now()
    start_date = end_date - timedelta(days=6)
    
    # Generate all dates in range
    date_range = []
    current_date = start_date
    while current_date <= end_date:
        date_range.append(current_date.strftime('%Y-%m-%d'))
        current_date += timedelta(days=1)
    
    # Query database for the date range
    pipeline = [
        {'$match': {
            'user_id': user_id,
            'date': {'$in': date_range}
        }},
        {'$group': {
            '_id': '$date',
            'total': {'$sum': '$calories'},
            'meal_counts': {'$sum': 1}
        }},
        {'$sort': {'_id': 1}}
    ]
    
    results = list(food_entries_collection.aggregate(pipeline))
    
    # Format data for chart
    daily_data = {date: {'calories': 0, 'meals': 0} for date in date_range}
    for item in results:
        daily_data[item['_id']] = {
            'calories': item['total'],
            'meals': item['meal_counts']
        }
    
    # Convert to list format for JS
    chart_data = [
        {
            'date': date, 
            'calories': data['calories'],
            'meals': data['meals'],
            'average_per_meal': round(data['calories'] / data['meals'], 1) if data['meals'] > 0 else 0
        } 
        for date, data in daily_data.items()
    ]
    
    # Sort by date
    chart_data.sort(key=lambda x: x['date'])
    
    # Calculate trend
    if len(chart_data) > 1:
        first_day = chart_data[0]['calories']
        last_day = chart_data[-1]['calories']
        trend_percentage = ((last_day - first_day) / max(first_day, 1)) * 100
        for entry in chart_data:
            entry['trend'] = round(trend_percentage, 1)
    
    return chart_data

@simple_cache(timeout=3600)  # Cache for 1 hour
def get_monthly_data(user_id):
    """Get daily calorie totals for the current month for heatmap visualization"""
    # Calculate date range for current month
    today = datetime.now()
    start_of_month = today.replace(day=1).strftime('%Y-%m-%d')
    end_of_month = today.strftime('%Y-%m-%d')
    
    # Query database for the date range
    pipeline = [
        {'$match': {
            'user_id': user_id,
            'date': {'$gte': start_of_month, '$lte': end_of_month}
        }},
        {'$group': {
            '_id': '$date',
            'total': {'$sum': '$calories'}
        }},
        {'$sort': {'_id': 1}}
    ]
    
    results = list(food_entries_collection.aggregate(pipeline))
    
    # Generate all dates in the month
    all_dates = []
    current = datetime.strptime(start_of_month, '%Y-%m-%d')
    end = datetime.strptime(end_of_month, '%Y-%m-%d')
    
    while current <= end:
        all_dates.append(current.strftime('%Y-%m-%d'))
        current += timedelta(days=1)
    
    # Format data for heatmap
    daily_data = {date: 0 for date in all_dates}
    for item in results:
        daily_data[item['_id']] = item['total']
    
    # Convert to list format for JS
    heatmap_data = [
        {'date': date, 'calories': calories} 
        for date, calories in daily_data.items()
    ]
    
    return heatmap_data

@app.route('/add_entry', methods=['POST'])
def add_entry():
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    food_name = request.form['food_name'].strip()
    
    # Improved validation
    try:
        calories = int(request.form['calories'])
        if calories < 0:
            flash('Calories cannot be negative', 'error')
            return redirect(url_for('dashboard'))
    except ValueError:
        flash('Calories must be a valid number', 'error')
        return redirect(url_for('dashboard'))
    
    meal_type = request.form['meal_type']
    if meal_type not in ['breakfast', 'lunch', 'dinner', 'snack']:
        flash('Invalid meal type', 'error')
        return redirect(url_for('dashboard'))
    
    if not food_name:
        flash('Food name cannot be empty', 'error')
        return redirect(url_for('dashboard'))
        
    date = datetime.now().strftime('%Y-%m-%d')
    
    # Optional nutrient information if provided
    nutrients = {}
    for nutrient in ['protein', 'carbs', 'fat']:
        if nutrient in request.form and request.form[nutrient].strip():
            try:
                nutrients[nutrient] = float(request.form[nutrient])
            except ValueError:
                # If invalid, just don't include it
                pass
    
    entry = {
        'user_id': session['user_id'],
        'food_name': food_name,
        'calories': calories,
        'meal_type': meal_type,
        'date': date,
        'created_at': datetime.now()
    }
    
    # Add nutrients if available
    if nutrients:
        entry['nutrients'] = nutrients
    
    try:
        food_entries_collection.insert_one(entry)
        flash('Food entry added successfully!', 'success')
    except PyMongoError as e:
        flash(f'Error adding entry: {str(e)}', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/delete_entry/<entry_id>', methods=['POST'])
def delete_entry(entry_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        result = food_entries_collection.delete_one({
            '_id': ObjectId(entry_id),
            'user_id': session['user_id']
        })
        
        if result.deleted_count == 1:
            flash('Entry deleted successfully', 'success')
        else:
            flash('Entry not found or you do not have permission to delete it', 'error')
            
    except Exception as e:
        flash(f'Error deleting entry: {str(e)}', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/update_goal', methods=['POST'])
def update_goal():
    if 'user_id' not in session:
        return jsonify({'error': 'Please log in first'}), 401
    
    try:
        new_goal = int(request.form['new_goal'])
        if new_goal < 500:
            flash('Daily goal must be at least 500 calories', 'error')
            return redirect(url_for('dashboard'))
            
        users_collection.update_one(
            {'_id': ObjectId(session['user_id'])},
            {'$set': {'daily_goal': new_goal}}
        )
        
        session['daily_goal'] = new_goal
        flash('Daily goal updated successfully', 'success')
        
    except ValueError:
        flash('Daily goal must be a valid number', 'error')
        
    except Exception as e:
        flash(f'Error updating goal: {str(e)}', 'error')
    
    return redirect(url_for('dashboard'))

@app.route('/history')
def history():
    if 'user_id' not in session:
        flash('Please log in first', 'error')
        return redirect(url_for('login'))
    
    try:
        # Group entries by date with enhanced aggregation
        pipeline = [
            {'$match': {'user_id': session['user_id']}},
            {'$group': {
                '_id': '$date',
                'total_calories': {'$sum': '$calories'},
                'entries': {'$push': {
                    'food_name': '$food_name',
                    'calories': '$calories',
                    'meal_type': '$meal_type',
                    'nutrients': {'$ifNull': ['$nutrients', {}]}
                }},
                'meal_breakdown': {
                    '$push': {
                        'k': '$meal_type',
                        'v': '$calories'
                    }
                }
            }},
            {'$sort': {'_id': -1}},
            {'$limit': 30}  # Limit to past 30 days for performance
        ]
        
        history_data = list(food_entries_collection.aggregate(pipeline))
        
        # Process meal breakdown
        for day in history_data:
            breakdown = {'breakfast': 0, 'lunch': 0, 'dinner': 0, 'snack': 0}
            for item in day['meal_breakdown']:
                breakdown[item['k']] += item['v']
            day['breakdown'] = breakdown
        
        return render_template('history.html', 
                            history=history_data,
                            daily_goal=session['daily_goal'],
                            name=session['name'])
    except PyMongoError as e:
        flash(f'Database error: {str(e)}', 'error')
        return render_template('history.html', 
                            history=[],
                            daily_goal=session['daily_goal'],
                            name=session['name'])

@app.route('/export_csv', methods=['POST'])
def export_csv():
    if 'user_id' not in session:
        flash('Please log in first', 'error')
        return redirect(url_for('login'))
    
    # Get date range from form parameters
    start_date = request.form.get('start_date', '')
    end_date = request.form.get('end_date', '')
    format_type = request.form.get('format_type', 'daily')
    
    # Validate dates
    try:
        if not start_date or not end_date:
            raise ValueError("Both start and end dates are required")
        
        # Ensure end date is not before start date
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        
        # Query database for entries in date range
        query = {
            'user_id': session['user_id'],
            'date': {'$gte': start_date, '$lte': end_date}
        }
        
        # Decide on sorting
        sort_order = [('date', 1), ('meal_type', 1)]
        
        # Execute query
        entries = list(food_entries_collection.find(query).sort(sort_order))
        
        # Handle case with no entries
        if not entries:
            flash('No entries found for the selected date range', 'warning')
            return redirect(url_for('export_csv_form'))
        
        # Create in-memory CSV file
        output = io.StringIO()
        writer = csv.writer(output)
        
        if format_type == 'daily':
            # Daily detailed format - each food entry on a separate row
            writer.writerow(['Date', 'Food Name', 'Meal Type', 'Calories', 'Protein (g)', 'Carbs (g)', 'Fat (g)'])
            
            for entry in entries:
                # Extract nutrient data if available
                nutrients = entry.get('nutrients', {})
                protein = nutrients.get('protein', '')
                carbs = nutrients.get('carbs', '')
                fat = nutrients.get('fat', '')
                
                writer.writerow([
                    entry['date'],
                    entry['food_name'],
                    entry['meal_type'],
                    entry['calories'],
                    protein,
                    carbs,
                    fat
                ])
        else:
            # Summary format - aggregate by date
            daily_summary = {}
            
            for entry in entries:
                date = entry['date']
                calories = entry['calories']
                
                if date not in daily_summary:
                    daily_summary[date] = {
                        'total_calories': 0,
                        'breakfast': 0,
                        'lunch': 0,
                        'dinner': 0,
                        'snack': 0,
                        'entries': 0
                    }
                
                daily_summary[date]['total_calories'] += calories
                daily_summary[date][entry['meal_type']] += calories
                daily_summary[date]['entries'] += 1
            
            # Write summary data
            writer.writerow(['Date', 'Total Calories', 'Breakfast', 'Lunch', 'Dinner', 'Snack', 'Number of Entries'])
            
            for date in sorted(daily_summary.keys()):
                data = daily_summary[date]
                writer.writerow([
                    date,
                    data['total_calories'],
                    data['breakfast'],
                    data['lunch'],
                    data['dinner'],
                    data['snack'],
                    data['entries']
                ])
        
        # Prepare response
        output.seek(0)
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-disposition": f"attachment; filename=calorie_tracker_{start_date}_to_{end_date}.csv"}
        )
    
    except ValueError as e:
        flash(f'Invalid date format: {str(e)}', 'error')
        return redirect(url_for('export_csv_form'))
    
    except PyMongoError as e:
        flash(f'Database error: {str(e)}', 'error')
        return redirect(url_for('export_csv_form'))

@app.route('/export_csv_form')
def export_csv_form():
    if 'user_id' not in session:
        flash('Please log in first', 'error')
        return redirect(url_for('login'))
        
    # Calculate default date range (current month)
    today = datetime.now()
    start_date = today.replace(day=1).strftime('%Y-%m-%d')
    end_date = today.strftime('%Y-%m-%d')
    
    return render_template('export_csv.html', 
                          start_date=start_date,
                          end_date=end_date)

@app.route('/nutrition_summary')
def nutrition_summary():
    """View nutritional breakdown over time."""
    if 'user_id' not in session:
        flash('Please log in first', 'error')
        return redirect(url_for('login'))
    
    # Get date range, default to current week
    end_date = datetime.now()
    start_date = end_date - timedelta(days=7)
    
    start_date_str = request.args.get('start_date', start_date.strftime('%Y-%m-%d'))
    end_date_str = request.args.get('end_date', end_date.strftime('%Y-%m-%d'))
    
    try:
        # Query for entries with nutrient information
        pipeline = [
            {'$match': {
                'user_id': session['user_id'],
                'date': {'$gte': start_date_str, '$lte': end_date_str},
                'nutrients': {'$exists': True}
            }},
            {'$group': {
                '_id': '$date',
                'total_calories': {'$sum': '$calories'},
                'total_protein': {'$sum': {'$ifNull': ['$nutrients.protein', 0]}},
                'total_carbs': {'$sum': {'$ifNull': ['$nutrients.carbs', 0]}},
                'total_fat': {'$sum': {'$ifNull': ['$nutrients.fat', 0]}}
            }},
            {'$sort': {'_id': 1}}
        ]
        
        nutrient_data = list(food_entries_collection.aggregate(pipeline))
        
        # Calculate totals and averages
        total_days = len(nutrient_data)
        if total_days > 0:
            overall_protein = sum(day['total_protein'] for day in nutrient_data)
            overall_carbs = sum(day['total_carbs'] for day in nutrient_data)
            overall_fat = sum(day['total_fat'] for day in nutrient_data)
            overall_calories = sum(day['total_calories'] for day in nutrient_data)
            
            avg_protein = overall_protein / total_days
            avg_carbs = overall_carbs / total_days
            avg_fat = overall_fat / total_days
            avg_calories = overall_calories / total_days
            
            # Calculate macronutrient percentages
            total_macro_calories = (overall_protein * 4) + (overall_carbs * 4) + (overall_fat * 9)
            protein_pct = (overall_protein * 4 / total_macro_calories * 100) if total_macro_calories > 0 else 0
            carbs_pct = (overall_carbs * 4 / total_macro_calories * 100) if total_macro_calories > 0 else 0
            fat_pct = (overall_fat * 9 / total_macro_calories * 100) if total_macro_calories > 0 else 0
        else:
            avg_protein = avg_carbs = avg_fat = avg_calories = 0
            protein_pct = carbs_pct = fat_pct = 0
        
        return render_template('nutrition_summary.html',
                            start_date=start_date_str,
                            end_date=end_date_str,
                            nutrient_data=nutrient_data,
                            daily_goal=session['daily_goal'],
                            avg_protein=round(avg_protein, 1),
                            avg_carbs=round(avg_carbs, 1),
                            avg_fat=round(avg_fat, 1),
                            avg_calories=round(avg_calories, 1),
                            protein_pct=round(protein_pct, 1),
                            carbs_pct=round(carbs_pct, 1),
                            fat_pct=round(fat_pct, 1),
                            chart_data=json.dumps(nutrient_data))
    except PyMongoError as e:
        flash(f'Database error: {str(e)}', 'error')
        return redirect(url_for('dashboard'))

if __name__ == '__main__':
    app.run(debug=True)